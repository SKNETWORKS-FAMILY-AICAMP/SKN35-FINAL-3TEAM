"""ApprovalHandler 구현 (CLI). Decision.ASK 일 때 터미널에서 y/N 으로 승인받는다."""
from __future__ import annotations

import json

from contracts.permission import PermissionResult, ToolCall


def _short(args: dict, limit: int = 300) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


class CliApprovalHandler:
    def request_approval(self, call: ToolCall, result: PermissionResult) -> bool:
        print(f"\n[승인 요청] {call.tool_name}  (user={call.principal.user_id})")
        print(f"  사유: {result.reason}  (rule={result.rule_id})")
        print(f"  인자: {_short(call.arguments)}")
        return input("  실행할까요? [y/N] ").strip().lower() in ("y", "yes")
