"""만기 대출 알림을 내 알림함으로 보낸다 (UC-03).

모델이 넘기는 것은 기간과 안내 문장뿐이다. 대출 목록은 여기서 다시 조회해 만든다 (모델이 바꾼 목록을 보내지 않도록).
안내 문장에 목록에 없는 숫자가 있으면 문장을 빼고 목록만 보낸다 (UC-03 5a).
받는 사람은 항상 로그인한 본인이다.
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall
from services.inbox import InboxStore
from tools.loans import LoanStore
from tools.number_check import unknown_numbers


class AlertInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    days: int = Field(default=30, ge=1, le=90, description="loans_maturing 을 부를 때 쓴 기간(일)")
    summary: str = Field(default="", max_length=300, description="오늘 먼저 할 일 안내 1~2문장. 목록에 있는 숫자만 쓴다")


SEND_MATURITY_ALERT_SPEC = ToolSpec(
    name="send_maturity_alert",
    display_name="만기 알림 보내기",
    effect="write",
    domain="여신",
    description=(
        "내 담당 만기 대출 목록을 내 알림함으로 보냅니다. 받는 사람은 항상 본인입니다. "
        "대출 목록은 이 도구가 직접 다시 조회해 붙이므로 목록을 넘기지 말고, 기간(days)과 안내 문장(summary)만 넘기세요. "
        "안내 문장에 목록에 없는 숫자가 있으면 문장은 빠지고 목록만 보내집니다. 고객에게 보내는 메일이 아닙니다."
    ),
    parameters=AlertInput.model_json_schema(),
)


class SendMaturityAlertTool:
    spec = SEND_MATURITY_ALERT_SPEC

    def __init__(self, loans: LoanStore, inbox: InboxStore) -> None:
        self.loans, self.inbox = loans, inbox

    def _send(self, call: ToolCall, arguments: AlertInput) -> dict:
        listing = self.loans.maturing(call.principal, arguments.days)
        if not listing["has_customers"]:
            return {"sent": False, "message": listing["message"]}
        bad = unknown_numbers(arguments.summary, json.dumps(listing, ensure_ascii=False))
        summary = "" if bad else arguments.summary
        title = f"{listing['as_of']} 기준 {arguments.days}일 이내 만기 대출 {listing['total']}건"
        alert = self.inbox.add(call.principal.user_id, "maturity", title, summary, bool(bad), listing)
        return {
            "sent": True, "alert_id": alert["alert_id"], "alert_title": title, "total": listing["total"],
            "counts": listing["counts"], "summary_dropped": bool(bad),
            "message": ("안내 문장에 목록에 없는 숫자(" + ", ".join(sorted(bad)) + ")가 있어 문장을 빼고 목록만 보냈습니다.")
            if bad else "알림함으로 보냈습니다.",
        }

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            arguments = AlertInput.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.call_id, False, "days 는 1~90 정수, summary 는 300자 이내로 입력해 주세요.")
        try:
            payload = await asyncio.to_thread(self._send, call, arguments)
        except (OSError, ValueError, KeyError):
            return ToolResult(call.call_id, False, "알림을 보내지 못했습니다. 여신 데이터와 알림함 저장소를 확인해 주세요.")
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
