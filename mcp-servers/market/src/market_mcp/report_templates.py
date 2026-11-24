"""양식 검색/전체 읽기/안전한 슬롯 채우기. 검색 저장소를 바꿔도 도구 계약은 유지한다."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ecos import MarketError

SLOTS = {"base_date", "summary", "fx_table", "rates", "data_quality", "source_notes", "prepared_by", "report_id"}
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["heading", "paragraph", "field", "binding"]
    text: str = Field(default="", max_length=20000)
    level: int = Field(default=2, ge=1, le=3)
    label: str = Field(default="", max_length=120)
    binding: str | None = None


class ReportTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    summary_instructions: str = Field(default="확인한 자료의 핵심만 요약합니다.", max_length=600)
    keywords: list[str] = Field(min_length=1, max_length=30)
    # 이전 사용자 정의 양식의 호환 읽기에만 사용한다. 새 양식은 sections로 정의한다.
    body_markdown: str | None = Field(default=None, min_length=1, max_length=20000)
    sections: list[ReportSection] | None = Field(default=None, min_length=1, max_length=100)
    required_bindings: list[str] = Field(default_factory=lambda: ["base_date", "source_notes"])
    demo: bool = True
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=9)


def validate_template(raw: dict) -> dict:
    try:
        value = ReportTemplate.model_validate(raw).model_dump(exclude_none=True)
    except ValidationError:
        raise MarketError("보고서 양식의 ID·버전·항목 형식을 확인해 주세요.") from None
    if ("sections" in value) == ("body_markdown" in value):
        raise MarketError("양식은 sections 또는 이전 body_markdown 중 하나로 정의해 주세요.")
    if "sections" in value:
        slots = set()
        for section in value["sections"]:
            if section["type"] in ("binding", "field"):
                if section.get("binding") not in SLOTS:
                    raise MarketError("양식에 지원하지 않는 입력 항목이 있습니다.")
                slots.add(section["binding"])
            elif not section["text"].strip() or section.get("binding"):
                raise MarketError("고정 제목·문단은 text로 작성해 주세요.")
        if sum(len(s["text"]) for s in value["sections"]) > 20000:
            raise MarketError("양식의 고정 문구는 총 20,000자 이내로 작성해 주세요.")
    else:
        slots = set(PLACEHOLDER.findall(value["body_markdown"]))
    if any(not word.strip() or len(word) > 100 for word in value["keywords"]):
        raise MarketError("양식 검색 키워드는 비어 있지 않은 100자 이내 문자열이어야 합니다.")
    if slots - SLOTS or set(value["required_bindings"]) - slots:
        raise MarketError("양식에 지원하지 않는 입력 항목이 있거나 필수 항목이 본문에 없습니다.")
    if not {"base_date", "source_notes"} <= slots:
        raise MarketError("보고서 양식에는 기준일과 출처 항목이 있어야 합니다.")
    leftover = PLACEHOLDER.sub("", value.get("body_markdown", ""))
    if "{{" in leftover or "}}" in leftover:
        raise MarketError("양식의 입력 위치는 {{base_date}}와 같은 형식이어야 합니다.")
    value["bindings"] = sorted(slots)
    value["hash"] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return value


class ReportTemplateCatalog:
    def __init__(self, directory: Path):
        self.directory = directory

    def all(self) -> list[dict]:
        if not self.directory.is_dir():
            raise MarketError("보고서 양식 폴더를 찾을 수 없습니다. REPORT_TEMPLATES_DIR를 확인해 주세요.")
        result, ids = [], set()
        for path in sorted(self.directory.glob("*.json")):
            try:
                value = validate_template(json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError, TypeError) as exc:
                raise MarketError(f"양식 파일 {path.name}을 읽을 수 없습니다. 형식과 필수 항목을 확인해 주세요.") from exc
            if value["id"] != path.stem or value["id"] in ids:
                raise MarketError("양식 파일 이름과 ID가 다르거나 ID가 중복되었습니다.")
            ids.add(value["id"])
            if value["enabled"]:
                result.append(value)
        return result

    def get(self, ident: str, expected_hash: str | None = None) -> dict:
        matches = [value for value in self.all() if value["id"] == ident]
        if not matches:
            raise MarketError("사용 가능한 보고서 양식을 찾을 수 없습니다.")
        value = matches[0]
        if expected_hash and value["hash"] != expected_hash:
            raise MarketError("검색 후 양식이 변경되었습니다. 양식을 다시 검색해 주세요.")
        return value

    def search(self, query: str) -> list[dict]:
        if not isinstance(query, str) or len(query) > 2000:
            raise MarketError("양식 검색어는 2,000자 이내로 입력해 주세요.")
        compact = re.sub(r"\s+", "", query.casefold())
        result = []
        for value in self.all():
            exact = value["id"] in query.casefold() or re.sub(r"\s+", "", value["name"]) in compact
            # 미리 정의된 검색용 키워드를 사용한다. 향후 이 부분을 벡터 검색 어댑터로 바꿀 수 있다.
            hits = [word for word in value["keywords"] if re.sub(r"\s+", "", word.casefold()) in compact]
            relevance = (1000 if exact else 0) + sum(2 + len(word) for word in hits)
            score = relevance * 10 + value["priority"]
            if relevance or not query.strip():
                result.append({"template_id": value["id"], "name": value["name"], "version": value["version"],
                               "description": value["description"], "template_hash": value["hash"], "score": score,
                               "matched_keywords": hits, "demo": value["demo"]})
        return sorted(result, key=lambda value: (-value["score"], value["template_id"]))


def render_template(template: dict, bindings: dict[str, str], available: set[str]) -> str:
    missing = set(template["required_bindings"]) - available
    if missing:
        raise MarketError("보고서 양식의 필수 자료가 부족합니다: " + ", ".join(sorted(missing)))
    # 한 번만 치환한다. 본문/요약에 들어 있는 {{...}}를 다시 명령처럼 해석하지 않는다.
    rendered = PLACEHOLDER.sub(lambda match: bindings[match[1]], template["body_markdown"])
    if template["demo"]:
        rendered = "> 테스트용 합성 양식입니다. 실제 은행 공식 서식이 아닙니다.\n\n" + rendered
    rendered += f"\n\n사용 양식: {template['name']} · 버전 {template['version']}"
    if "report_id" not in template["bindings"]:
        rendered += "\n리포트 번호: " + bindings["report_id"]
    return rendered
