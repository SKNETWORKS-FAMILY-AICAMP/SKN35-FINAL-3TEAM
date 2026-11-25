"""만기 고객 안내문 준비 (UC-05).

고객에게 나가는 글이라 본문은 모델이 쓰지 않고 코드 템플릿으로 채운다.
- prepare_maturity_notices(days): 보낼 안내문 목록을 보여 준다 (보내지 않음).
- send_maturity_notice(loan_id): 안내문 1통을 코드로 다시 만들어 Gmail MCP send_mail 로 보낸다.
  모델은 대출 번호만 넘긴다. qwen3 8b·14b 모두 긴 본문을 send_mail 인자로 옮겨 적지 못하고
  "보냈다"는 말만 했다 (2026-11-23 실측). 짧은 인자로는 툴을 잘 부른다.
  승인 카드에는 받는 사람·제목·본문이 보인다 (permission/customer_mail.py 가 이유 칸에 채움).
고객 이메일로 send_mail 을 직접 쓰는 것은 permission/customer_mail.py 가 막는다 (이 도구로만 보낸다).
양식(문구·필수 고지)은 은행 내규가 정해지면 render_notice 만 바꾼다.

중복 발송 막기 (services/customer_notices.py): 같은 대출·같은 만기일 안내는 한 번만.
- 준비 단계에서 이미 보냈거나 보내는 중인 건은 already 로 빠진다 (UC-05-1 3a).
- 승인 뒤 발송 직전에 자리를 잡고(reserve), 보낸 뒤 sent 로 적는다. 응답이 끊겨 결과를 모르면 sending 으로 둔다.
- 반려·시간 초과도 기록하고 (on_not_approved), 보냄·반려 결과는 직원 알림함에 남긴다 (UC-05-2).
"""

from __future__ import annotations

import asyncio
import json
import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall
from tools.loans import OVERDUE, LoanStore

SEND_TOOL = "send_maturity_notice"
log = logging.getLogger("uvicorn.error")

SUBJECT = "[대출 만기 안내] {product} 만기일 안내"


def render_notice(item: dict) -> dict:
    """대출 1건 → 메일 1통. 같은 입력이면 항상 같은 글이 나온다 (본문 검사가 이걸 다시 만들어 비교한다)."""
    date = item["maturity_date"]
    when = (f"이용 중이신 {item['product']}의 만기일({date})이 지났습니다."
            if item["bucket"] == OVERDUE else
            f"이용 중이신 {item['product']}의 만기일은 {date}입니다.")
    body = "\n".join([
        "고객님, 안녕하세요.",
        "",
        when,
        f"대출 잔액: {item['balance']}",
        "",
        "기한연장 또는 상환을 원하시면 담당 직원에게 연락해 주시기 바랍니다.",
        "기한연장 가능 여부와 조건은 심사를 거쳐 정해지며, 이 메일은 연장이나 금리 조건을 약속하지 않습니다.",
        "",
        "감사합니다.",
    ])
    return {"to": [item["email"]], "subject": SUBJECT.format(product=item["product"]), "body": body}


class NoticeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    days: int = Field(default=7, ge=1, le=90, description="오늘부터 며칠 안에 만기가 오는 고객에게 보낼지 (1~90). 만기가 지난 대출도 포함")


PREPARE_NOTICES_SPEC = ToolSpec(
    name="prepare_maturity_notices",
    display_name="만기 안내문 준비",
    effect="read",
    domain="여신",
    description=(
        "내 담당 대출 중 기간 안에 만기가 오는 고객에게 보낼 만기 안내 메일을 고객 한 명당 한 통씩 만들어 돌려줍니다. "
        "보내려면 notices 의 loan_id 마다 send_maturity_notice 를 한 번씩 호출하세요. "
        "이메일이 없는 고객은 skipped, 이미 보냈거나 보내는 중인 고객은 already 에 들어가며 다시 보내지 않습니다. 이 도구는 메일을 보내지 않습니다."
    ),
    parameters=NoticeInput.model_json_schema(),
)


class PrepareMaturityNoticesTool:
    spec = PREPARE_NOTICES_SPEC

    def __init__(self, loans: LoanStore, notices=None) -> None:
        self.loans, self.notices = loans, notices

    async def _prepare(self, call: ToolCall, days: int) -> dict:
        listing = await asyncio.to_thread(self.loans.maturing, call.principal, days)
        if not listing["has_customers"]:
            return {"notices": [], "skipped": [], "already": [], "message": listing["message"]}
        notices, already = [], []
        for item in listing["items"]:
            if not item["email"]:
                continue
            record = await self.notices.active(item["loan_id"], item["maturity_date"]) if self.notices else None
            if record is not None:
                already.append({"loan_id": item["loan_id"], "customer": item["customer"],
                                "maturity_date": item["maturity_date"],
                                "status": "이미 발송함" if record["status"] == "sent" else "발송 중(결과 확인 필요)",
                                "at": (record.get("sent_at") or record.get("created_at") or "")[:10]})
                continue
            notices.append({"loan_id": item["loan_id"], "customer": item["customer"],
                            "maturity_date": item["maturity_date"], **render_notice(item)})
        skipped = [{"customer": item["customer"], "product": item["product"], "maturity_date": item["maturity_date"],
                    "reason": "이메일 없음"} for item in listing["items"] if not item["email"]]
        return {
            "as_of": listing["as_of"], "days": days, "count": len(notices), "notices": notices, "skipped": skipped,
            "already": already,
            "next_step": "보내려면 send_maturity_notice 를 loan_id " + ", ".join(n["loan_id"] for n in notices) + " 마다 한 번씩 호출한다."
            if notices else "보낼 메일이 없다.",
        }

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            arguments = NoticeInput.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.call_id, False, "days 는 1~90 사이 정수로 입력해 주세요.")
        try:
            payload = await self._prepare(call, arguments.days)
        except (OSError, ValueError, KeyError, SQLAlchemyError):
            return ToolResult(call.call_id, False, "여신 데이터 또는 발송 기록을 읽을 수 없습니다.")
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)


class SendNoticeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    loan_id: str = Field(min_length=1, max_length=40, description="prepare_maturity_notices 결과의 loan_id. 예: L-1002")


SEND_NOTICE_SPEC = ToolSpec(
    name=SEND_TOOL,
    display_name="만기 안내 메일 보내기",
    effect="send",
    domain="여신",
    description=(
        "내 담당 대출 1건의 고객에게 만기 안내 메일 1통을 본인 메일 계정으로 보냅니다. 대출 번호(loan_id)만 넘기면 "
        "안내문은 정해진 양식으로 이 도구가 만듭니다. 사용자가 승인한 뒤에 발송됩니다. 고객마다 한 번씩 호출하세요."
    ),
    parameters=SendNoticeInput.model_json_schema(),
)


class SendMaturityNoticeTool:
    spec = SEND_NOTICE_SPEC

    def __init__(self, loans: LoanStore, mailer=None, notices=None, inbox=None) -> None:
        self.loans = loans
        self.mailer = mailer  # Gmail MCP 의 send_mail 툴 (tools/gmail.py). 없으면 보낼 수 없다
        self.notices = notices  # 발송 기록 (services/customer_notices.py). 없으면 중복 검사를 하지 않는다 (테스트용)
        self.inbox = inbox  # 결과를 직원 알림함에 남긴다

    async def _item(self, call: ToolCall) -> dict | None:
        loan_id = call.arguments.get("loan_id")
        if not isinstance(loan_id, str):
            return None
        item = await asyncio.to_thread(self.loans.own_item, call.principal, loan_id.strip())
        return item if item and item["email"] else None

    async def _notify(self, call: ToolCall, item: dict, title: str, summary: str, status: str) -> None:
        if self.inbox is None:
            return
        try:
            await self.inbox.add(call.principal.user_id, "notice_result", title, summary, False,
                                 {"loan_id": item["loan_id"], "customer": item["customer"], "status": status,
                                  "maturity_date": item["maturity_date"], "subject": render_notice(item)["subject"]},
                                 has_customer_info=True, session_id=call.session_id)
        except (OSError, ValueError, SQLAlchemyError):
            log.warning("발송 결과를 알림함에 남기지 못했습니다 (%s)", item["loan_id"], exc_info=True)

    async def on_not_approved(self, call: ToolCall, outcome: str) -> None:
        """하네스가 승인 카드에서 반려·시간 초과되면 부른다. 보내지 않았으므로 다시 요청할 수 있다 (UC-05-2 2a)."""
        try:
            item = await self._item(call)
        except (OSError, ValueError, KeyError):
            item = None
        if item is None:
            return
        reason = "반려" if outcome == "rejected" else "승인 시간 초과"
        notice = render_notice(item)
        if self.notices is not None:
            try:
                await self.notices.reject(call.principal.user_id, item["loan_id"], item["maturity_date"],
                                          notice["to"][0], notice["subject"], call.call_id, reason)
            except (OSError, ValueError, SQLAlchemyError):
                log.warning("반려 기록을 남기지 못했습니다 (%s)", item["loan_id"], exc_info=True)
        await self._notify(call, item, f"고객 안내 {reason}: {item['customer']} ({item['loan_id']})",
                           "승인되지 않아 메일을 보내지 않았습니다. 필요하면 다시 요청할 수 있습니다.", outcome)

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            arguments = SendNoticeInput.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.call_id, False, "loan_id 를 문자열로 입력해 주세요.")
        if self.mailer is None:
            return ToolResult(call.call_id, False, "메일 도구(Gmail MCP)가 연결되어 있지 않아 보낼 수 없습니다.")
        try:  # 승인을 기다리는 동안 데이터가 바뀌었을 수 있으니 다시 읽는다
            item = await asyncio.to_thread(self.loans.own_item, call.principal, arguments.loan_id)
        except (OSError, ValueError, KeyError):
            return ToolResult(call.call_id, False, "여신 데이터를 읽을 수 없습니다.")
        if item is None or not item["email"]:
            return ToolResult(call.call_id, False, "보낼 수 있는 내 담당 대출을 찾을 수 없습니다.")
        notice = render_notice(item)
        notice_id = None
        if self.notices is not None:
            try:
                notice_id = await self.notices.reserve(call.principal.user_id, item["loan_id"], item["maturity_date"],
                                                       notice["to"][0], notice["subject"], call.call_id)
            except (OSError, ValueError, SQLAlchemyError):
                return ToolResult(call.call_id, False, "발송 기록을 남길 수 없어 보내지 않았습니다.")
            if notice_id is None:
                return ToolResult(call.call_id, False,
                                  f"{item['customer']} 고객({item['loan_id']})에게는 이 만기일 안내를 이미 보냈거나 보내는 중이라 다시 보내지 않았습니다.")
        sent = await self.mailer.run(ToolCall(call.call_id, "send_mail", {**notice, "cc": []}, call.principal,
                                              call.session_id, call.agent_id, call.run_id))
        if not sent.ok:
            unsure = "불확실" in sent.content
            if notice_id is not None and not unsure:  # 결과를 모르면 sending 으로 두어 다시 보내지 않는다
                await self.notices.finish(notice_id, "failed", error=sent.content[:500])
            await self._notify(call, item, f"고객 안내 발송 {'확인 필요' if unsure else '실패'}: {item['customer']} ({item['loan_id']})",
                               sent.content[:300], "unknown" if unsure else "failed")
            return sent
        message_id = (sent.data or {}).get("message_id") if isinstance(sent.data, dict) else None
        if notice_id is not None:
            await self.notices.finish(notice_id, "sent", message_id=message_id)
        await self._notify(call, item, f"고객 안내 발송: {item['customer']} ({item['loan_id']})",
                           f"{notice['to'][0]} 로 \"{notice['subject']}\" 메일을 보냈습니다.", "sent")
        payload = {"sent": True, "loan_id": item["loan_id"], "customer": item["customer"], "to": notice["to"],
                   "subject": notice["subject"], "mail": sent.data}
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
