"""Submit a local review request after the harness approval step."""

import asyncio
import json
import sqlite3

from pydantic import ConfigDict, Field, ValidationError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall
from .get_review_case import ReviewLookupInput
from .review_store import ReviewStore


class ReviewSubmissionInput(ReviewLookupInput):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    notes: str = Field(default="", max_length=1000, description="심사 담당자에게 전달할 메모")


SUBMIT_REVIEW_REQUEST_SPEC = ToolSpec(
    name="submit_review_request",
    display_name="심사 요청 등록",
    effect="write",
    domain="심사",
    description=(
        "기존 심사 건을 로컬 가상 심사시스템에 등록합니다. 먼저 get_review_case로 상태와 누락 서류를 확인하고 "
        "사용자의 등록 요청이 있을 때 호출하세요. 승인 후 등록되며 누락 서류는 요청에 함께 기록됩니다. "
        "같은 건·요청자·메모의 재시도는 기존 요청을 반환합니다. 대출 승인 판정을 수행하지 않습니다. "
        "대출 심사 요청 전용이며, 고객 안내 메일 발송이나 다른 업무의 승인 요청에는 쓰지 않습니다."
    ),
    parameters=ReviewSubmissionInput.model_json_schema(),
)


class SubmitReviewRequestTool:
    spec = SUBMIT_REVIEW_REQUEST_SPEC

    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            arguments = ReviewSubmissionInput.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.call_id, False, "case_id는 1~100자, notes는 1000자 이내 문자열로 입력해 주세요.")
        try:
            payload = await asyncio.to_thread(
                self.store.submit, arguments.case_id, arguments.notes, call.principal,
            )
        except (OSError, UnicodeError, sqlite3.Error):
            return ToolResult(call.call_id, False, "심사 요청을 저장할 수 없습니다. 조회 후 다시 확인해 주세요.")
        except ValueError as error:
            message = "심사 데이터 형식을 확인해 주세요." if isinstance(error, (ValidationError, json.JSONDecodeError)) else str(error)
            return ToolResult(call.call_id, False, message)
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
