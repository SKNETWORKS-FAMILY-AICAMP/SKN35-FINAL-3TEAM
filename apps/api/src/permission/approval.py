"""ASK 판정 승인 처리 (COM-07, SYS-16).

하네스가 request_approval() 을 await 하면 Future 를 만들어 두고 기다린다.
웹 쪽은 pending() 으로 대기 중인 요청을 보여주고, 사용자가 누르면 resolve() 를 부른다.
timeout 안에 응답이 없으면 "timeout" 을 돌려준다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from contracts.permission import ApprovalOutcome, PermissionResult, ToolCall

OnRequest = Callable[["PendingApproval"], Awaitable[None]]


@dataclass(frozen=True)
class PendingApproval:
    call: ToolCall
    result: PermissionResult


class FutureApprovalHandler:
    def __init__(self, timeout_seconds: float, on_request: OnRequest | None = None) -> None:
        self._timeout = timeout_seconds
        self._on_request = on_request  # 예: 스트리밍으로 승인 카드를 띄우는 콜백
        self._waiting: dict[str, tuple[PendingApproval, asyncio.Future[bool]]] = {}

    def pending(self, session_id: str | None = None) -> list[PendingApproval]:
        items = [p for p, _ in self._waiting.values()]
        return [p for p in items if session_id is None or p.call.session_id == session_id]

    def resolve(self, call_id: str, approved: bool, *, user_id: str | None = None) -> bool:
        """승인/거절을 전달한다. 대기 중인 요청이 없으면 False.

        user_id 를 주면 요청자 본인만 응답할 수 있다.
        """
        entry = self._waiting.get(call_id)
        if entry is None:
            return False
        pending, fut = entry
        if user_id is not None and pending.call.principal.user_id != user_id:
            return False
        if not fut.done():
            fut.set_result(bool(approved))
        return True

    async def request_approval(self, call: ToolCall, result: PermissionResult) -> ApprovalOutcome:
        if call.call_id in self._waiting:
            raise ValueError(f"call_id '{call.call_id}' 승인 요청이 이미 대기 중")
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        pending = PendingApproval(call, result)
        self._waiting[call.call_id] = (pending, fut)
        try:
            if self._on_request is not None:
                await self._on_request(pending)
            approved = await asyncio.wait_for(fut, timeout=self._timeout)
        except TimeoutError:
            return "timeout"
        finally:
            self._waiting.pop(call.call_id, None)
        return "approved" if approved else "rejected"
