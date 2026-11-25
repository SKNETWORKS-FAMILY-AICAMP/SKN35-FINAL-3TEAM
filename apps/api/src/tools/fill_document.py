"""양식으로 문서를 만든다. 서류가 나오는 작업은 모두 이 툴 하나를 쓴다 (새 서류 = 새 양식 JSON).

모델이 넘기는 것: 양식 id, 조회 인자(params), 문장 칸(fields). 표·숫자 칸은 코드가 조회 결과로 채운다.
결과의 document_id 를 화면이 보고 [PDF] [DOCX] 내려받기 버튼을 붙인다. 파일 변환은 툴이 아니라 내려받기 API 가 한다.
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall
from services.documents import DocumentStore, Provider, build_document
from tools.number_check import unknown_numbers


class FillInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    form_id: str = Field(min_length=1, max_length=64, description="양식 id")
    params: dict[str, int] = Field(default_factory=dict, description="양식의 params 값. 정수만 (예: {\"days\": 30})")
    fields: dict[str, str] = Field(default_factory=dict, description="양식의 fields 칸에 쓸 문장 (예: {\"summary\": \"...\"})")


class FillDocumentTool:
    def __init__(self, store: DocumentStore, providers: dict[str, Provider]) -> None:
        self.store = store
        self.providers = providers
        forms = "; ".join(form.guide() for form in store.forms.values())
        self.spec = ToolSpec(
            name="fill_document",
            display_name="문서 만들기",
            effect="write",
            domain="문서",
            description=(
                "정해진 양식으로 문서를 만들어 저장합니다. 표와 숫자는 이 도구가 조회 결과로 직접 채우므로 넘기지 말고, "
                "params 와 fields(문장 칸)만 넘기세요. 사용자는 화면의 PDF·DOCX 버튼으로 문서를 받습니다. "
                f"쓸 수 있는 양식: {forms or '없음'}"
            ),
            parameters=FillInput.model_json_schema(),
        )

    def _params(self, form, raw: dict) -> dict:
        params = {}
        for key, rule in form.params.items():
            value = raw.get(key, rule.get("default"))
            if rule.get("type") == "integer":
                if type(value) is not int or not rule.get("min", value) <= value <= rule.get("max", value):
                    raise ValueError(f"params.{key} 는 {rule.get('min')}~{rule.get('max')} 사이 정수여야 합니다.")
            params[key] = value
        if set(raw) - set(form.params):
            raise ValueError(f"이 양식에 없는 params 입니다: {', '.join(sorted(set(raw) - set(form.params)))}")
        return params

    async def _fill(self, call: ToolCall, args: FillInput) -> dict:
        form = self.store.forms.get(args.form_id)
        if form is None:
            raise ValueError(f"없는 양식입니다. 쓸 수 있는 양식: {', '.join(self.store.forms) or '없음'}")
        params = self._params(form, args.params)
        llm = {s["key"]: s for s in form.llm_sections}
        if set(args.fields) - set(llm):
            raise ValueError(f"모델이 쓸 수 있는 칸은 {', '.join(llm) or '없음'} 뿐입니다. 표·숫자 칸은 넘기지 마세요.")
        data = await asyncio.to_thread(self.providers[form.provider], call.principal, params)
        if data.get("has_customers") is False or data.get("empty"):
            return {"created": False, "message": data.get("message") or "문서에 넣을 데이터가 없습니다."}
        source = json.dumps({"data": data, "params": params}, ensure_ascii=False)
        fields, dropped = {}, []
        for key, text in args.fields.items():
            rule = llm[key]
            text = text.strip()[: rule.get("max_chars", 500)]
            bad = unknown_numbers(text, source) if rule.get("check_numbers") else set()
            if bad:
                dropped.append({"field": key, "reason": "조회 결과에 없는 숫자: " + ", ".join(sorted(bad))})
            else:
                fields[key] = text
        document = build_document(form, data, params, fields, prepared_by=call.principal.user_id)
        record = await self.store.save(call.principal.user_id, document, form_id=form.form_id,
                                       has_customer_info=form.has_customer_info, session_id=call.session_id)
        return {
            "created": True, "document_id": record["document_id"], "document_title": document["title"],  # "title" 이면 채팅이 참고 문서로 착각한다 (services/chat.sources)
            "dropped": dropped,
            "facts": {k: data[k] for k in ("as_of", "total", "counts") if k in data}
            | ({"first_look": [{k: i[k] for k in ("customer", "maturity_date", "follow_up")} for i in data["first_look_items"]]}
               if "first_look_items" in data else {}),
            "message": "문서를 만들었습니다. 사용자는 채팅 화면의 PDF·DOCX 버튼으로 받습니다."
            + (" 일부 문장 칸은 조회 결과에 없는 숫자가 있어 비웠습니다." if dropped else ""),
        }

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            args = FillInput.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.call_id, False, "form_id(문자열), params(객체), fields(문자열 값 객체)로 입력해 주세요.")
        try:
            payload = await self._fill(call, args)
        except ValueError as error:
            return ToolResult(call.call_id, False, str(error))
        except (OSError, KeyError, SQLAlchemyError):
            return ToolResult(call.call_id, False, "문서를 만들지 못했습니다. 양식과 데이터를 확인해 주세요.")
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
