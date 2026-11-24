"""고객 메일 수신자 검사 (UC-05). 승인 카드를 띄우기 전에 코드로 막는다.

- 고객 한 명당 한 통: 받는 사람·참조에 고객 이메일이 둘 이상이면 거부.
- 내 담당 고객에게만: 다른 담당자의 고객 이메일이 있으면 거부.
- 고객 안내는 send_maturity_notice 로만: 고객 이메일이 들어간 send_mail 은 거부한다.
  안내문은 코드 양식으로만 만들고, 발송 기록(중복 발송 막기)이 한 곳에 남게 한다.
- 한 번만: 같은 대출·같은 만기일 안내를 이미 보냈거나 보내는 중이면 승인 카드 없이 거부한다.
고객 이메일이 없는 메일(동료에게 보내는 메일 등)은 원래 정책(config/permissions.yaml)으로 넘긴다.

send_maturity_notice(loan_id) 는 인자에 본문이 없으므로, 여기서 안내문을 만들어 승인 카드의 이유 칸에 받는 사람·제목·본문을 채운다.
관리자도 예외 없다 (정책의 admin-all 보다 먼저 본다).
"""

from __future__ import annotations

import asyncio

from contracts.permission import Decision, PermissionChecker, PermissionResult, ToolCall
from tools.loans import LoanStore
from tools.maturity_notice import SEND_TOOL, render_notice

MAIL_TOOL = "send_mail"


class CustomerMailRule:
    def __init__(self, inner: PermissionChecker, loans: LoanStore, notices=None) -> None:
        self.inner = inner
        self.loans = loans
        self.notices = notices  # 발송 기록 (services/customer_notices.py)

    async def check(self, call: ToolCall) -> PermissionResult:
        if call.tool_name == MAIL_TOOL:
            denied = await self._check_recipients(call)
            if denied:
                return denied
        if call.tool_name == SEND_TOOL:
            return await self._notice_card(call)
        return await self.inner.check(call)

    async def _notice_card(self, call: ToolCall) -> PermissionResult:
        loan_id = call.arguments.get("loan_id")
        try:
            item = await asyncio.to_thread(self.loans.own_item, call.principal, loan_id) if isinstance(loan_id, str) else None
        except (OSError, ValueError, KeyError):
            item = None
        if item is None or not item["email"]:
            return PermissionResult(Decision.DENY, "보낼 수 있는 내 담당 대출이 아닙니다 (대출 번호, 담당자, 고객 이메일을 확인해 주세요).",
                                    "maturity-notice-own-only")
        if self.notices is not None:
            try:
                record = await self.notices.active(item["loan_id"], item["maturity_date"])
            except Exception:  # 기록을 못 읽으면 보내지 않는다
                return PermissionResult(Decision.DENY, "발송 기록을 확인할 수 없어 보낼 수 없습니다.", "maturity-notice-log-unavailable")
            if record is not None:
                when = (record.get("sent_at") or record.get("created_at") or "")[:10]
                state = f"{when}에 이미 보냈습니다" if record["status"] == "sent" else "보내는 중이거나 결과를 확인하지 못했습니다 (보낸편지함을 확인해 주세요)"
                return PermissionResult(Decision.DENY, f"{item['customer']} 고객({item['loan_id']})의 이 만기일 안내는 {state}. 같은 안내는 한 번만 보냅니다.",
                                        "maturity-notice-once")
        notice = render_notice(item)
        reason = (f"고객에게 만기 안내 메일이 본인 메일 계정으로 발송됩니다. 내용을 확인해 주세요.\n"
                  f"받는 사람: {notice['to'][0]} ({item['customer']})\n제목: {notice['subject']}\n\n{notice['body']}")
        return PermissionResult(Decision.ASK, reason, "maturity-notice-confirm")

    async def _check_recipients(self, call: ToolCall) -> PermissionResult | None:
        recipients = [
            value.strip().lower()
            for key in ("to", "cc")
            for value in (call.arguments.get(key) or [])
            if isinstance(value, str)
        ]
        try:
            owners = await asyncio.to_thread(self.loans.customer_emails)  # 이메일(소문자) → 담당자 ID
        except (OSError, ValueError, KeyError):
            return PermissionResult(Decision.DENY, "고객 정보를 확인할 수 없어 메일을 보낼 수 없습니다.", "customer-mail-unknown")
        customers = list(dict.fromkeys(r for r in recipients if r in owners))
        if len(customers) > 1:
            return PermissionResult(
                Decision.DENY,
                "고객 안내 메일은 고객 한 명당 한 통씩 보냅니다. 받는 사람·참조에 다른 고객을 함께 넣을 수 없습니다.",
                "customer-mail-one-per-customer",
            )
        if not customers:
            return None
        if owners[customers[0]] != call.principal.user_id:
            return PermissionResult(Decision.DENY, "내가 담당하는 고객에게만 안내 메일을 보낼 수 있습니다.", "customer-mail-own-only")
        return PermissionResult(
            Decision.DENY,
            "고객 안내 메일은 '만기 안내 메일 보내기'(send_maturity_notice)로 대출 번호만 넘겨 보냅니다. "
            "정해진 양식과 발송 기록(같은 안내는 한 번만)이 그 도구에만 있습니다.",
            "customer-mail-notice-only",
        )
