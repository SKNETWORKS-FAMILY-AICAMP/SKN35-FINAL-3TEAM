"""Look up a review case through the local review-system adapter."""

import asyncio
import json
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall
from .review_store import ReviewStore, find_review_case, load_review_cases


class ReviewLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    case_id: str = Field(min_length=1, max_length=100, description="심사 건 번호. 예: REVIEW-001")


GET_REVIEW_CASE_SPEC = ToolSpec(
    name="get_review_case",
    display_name="심사 건 조회",
    effect="read",
    domain="심사",
    description="가상 심사시스템에서 요청자의 부서와 보안등급으로 열람 가능한 심사 건의 상태, 제출·누락 서류와 등록 요청을 조회합니다.",
    parameters=ReviewLookupInput.model_json_schema(),
)


class GetReviewCaseTool:
    spec = GET_REVIEW_CASE_SPEC

    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            arguments = ReviewLookupInput.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.call_id, False, "case_id를 1~100자의 문자열로 입력해 주세요.")
        try:
            payload = await asyncio.to_thread(self.store.get_case, arguments.case_id, call.principal)
        except (OSError, UnicodeError, sqlite3.Error):
            return ToolResult(call.call_id, False, "심사 데이터를 읽을 수 없습니다.")
        except ValueError as error:
            message = "심사 데이터 형식을 확인해 주세요." if isinstance(error, (ValidationError, json.JSONDecodeError)) else str(error)
            return ToolResult(call.call_id, False, message)
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
