"""커스텀 에이전트 저장소와 조회·관리 권한.

권한 규칙 (2026-11-19 합의):
- 일반 사용자: 자기 에이전트 + 공유받은 에이전트(같은 부서의 dept 범위, company 범위)를 본다.
  관리는 자기 것만 한다.
- 부서장: 위에 더해 부서원이 만든 에이전트를 본다. 그 에이전트는 비활성화·재활성화만 할 수 있다.
- admin: 모든 에이전트를 보고 관리한다.
역할·부서는 서버가 채운 Principal 에서만 읽는다.

공유 범위(scope)는 바로 위 관리자의 승인으로만 넓어진다 (ShareRequest = agent_share_request).
- 만들 때는 항상 personal 이고, 수정 API 로는 바꿀 수 없다.
- 팀원 → 부서장, 부서장 → admin 이 승인한다. admin 이 만든 에이전트는 바로 바뀐다.
- admin 은 모든 요청을 승인·반려할 수 있다.
- 공유를 거두는 것(personal 로 되돌리기)은 승인 없이 주인·admin 이 바로 한다.
- 공유 중이거나 승인 대기 중인 에이전트는 아무도(admin 포함) 수정할 수 없다. 승인받은 내용 그대로 쓰게 하려는 것.
  고치려면 공유를 해제하고 고친 뒤 다시 요청한다. 비활성화·삭제·공유 해제는 그대로 된다.

저장소: 메모리(InMemory*, 기본·테스트) 또는 DB(services/db_stores.py, STORE=db). 둘 다 같은 async 메서드.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

from contracts.permission import Principal

Scope = Literal["personal", "dept", "company"]
SharedScope = Literal["dept", "company"]
Status = Literal["active", "disabled"]
RequestStatus = Literal["pending", "approved", "rejected"]
SCOPE_ORDER = {"personal": 0, "dept": 1, "company": 2}

ADMIN = "admin"
DEPT_HEAD = "dept_head"


@dataclass(frozen=True)
class Agent:
    agent_id: str
    owner_user_id: str
    owner_dept_id: object  # 만든 사람의 부서 (DB 에서는 users 조인)
    name: str
    system_prompt: str
    tools: tuple[str, ...] = ()
    description: str = ""
    scope: Scope = "personal"
    status: Status = "active"
    disabled_by: str | None = None  # 누가 비활성화했는지 (주인이 직접 끈 것만 주인이 다시 켤 수 있다)


# ── 권한 판단 ───────────────────────────────────────────────

def _dept(p: Principal) -> object:
    return p.attributes.get("dept_id")


def _is_admin(p: Principal) -> bool:
    return ADMIN in p.roles


def _is_owner(p: Principal, a: Agent) -> bool:
    return a.owner_user_id == p.user_id


def _heads_owner_dept(p: Principal, a: Agent) -> bool:
    return DEPT_HEAD in p.roles and _dept(p) is not None and _dept(p) == a.owner_dept_id


def _shared_with(p: Principal, a: Agent) -> bool:
    if a.status != "active":
        return False
    if a.scope == "company":
        return True
    return a.scope == "dept" and _dept(p) is not None and _dept(p) == a.owner_dept_id


def can_view(p: Principal, a: Agent) -> bool:
    return _is_admin(p) or _is_owner(p, a) or _heads_owner_dept(p, a) or _shared_with(p, a)


def can_use(p: Principal, a: Agent) -> bool:
    """채팅에 쓸 수 있는가. 비활성화된 에이전트는 아무도 못 쓴다."""
    return a.status == "active" and can_view(p, a)


def can_edit(p: Principal, a: Agent) -> bool:
    """이름·설명·프롬프트·툴 수정, 삭제를 할 수 있는 사람인가 (공유 중 편집 잠금은 routers/agents.py)."""
    return _is_admin(p) or _is_owner(p, a)


def can_disable(p: Principal, a: Agent) -> bool:
    return can_edit(p, a) or _heads_owner_dept(p, a)


def can_enable(p: Principal, a: Agent) -> bool:
    if _is_admin(p) or _heads_owner_dept(p, a):
        return True
    # 부서장·관리자가 끈 것을 주인이 다시 켜면 관리 의미가 없다
    return _is_owner(p, a) and a.disabled_by in (None, p.user_id)


# ── 저장소 ─────────────────────────────────────────────────

@dataclass
class InMemoryAgentStore:
    _agents: dict[str, Agent] = field(default_factory=dict)

    async def create(
        self, owner: Principal, *, name: str, system_prompt: str,
        tools: tuple[str, ...] = (), description: str = "",
    ) -> Agent:
        a = Agent(
            agent_id=str(uuid.uuid4()),
            owner_user_id=owner.user_id,
            owner_dept_id=_dept(owner),
            name=name,
            system_prompt=system_prompt,
            tools=tools,
            description=description,
        )
        self._agents[a.agent_id] = a
        return a

    async def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    async def visible_to(self, p: Principal) -> list[Agent]:
        return [a for a in self._agents.values() if can_view(p, a)]

    async def save(self, agent: Agent) -> Agent:
        self._agents[agent.agent_id] = agent
        return agent

    async def update(self, agent: Agent, **changes: object) -> Agent:
        return await self.save(replace(agent, **changes))

    async def delete(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)


# ── 공유 요청 (agent_share_request) ─────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ShareRequest:
    request_id: str
    agent_id: str
    requester_id: str
    approver_id: str  # 바로 위 관리자 (admin 은 이 값과 상관없이 결정할 수 있다)
    requested_scope: SharedScope
    comment: str = ""  # 요청 사유
    status: RequestStatus = "pending"
    decided_by: str | None = None
    decision_comment: str = ""  # 승인·반려 의견
    requested_at: datetime = field(default_factory=_now)
    decided_at: datetime | None = None


def can_request_share(p: Principal, a: Agent) -> bool:
    return _is_owner(p, a)  # 주인만 공유를 요청한다 (admin 도 남의 에이전트는 직접 범위를 바꾸지 않는다)


def can_unshare(p: Principal, a: Agent) -> bool:
    return can_edit(p, a)


def can_decide(p: Principal, r: ShareRequest) -> bool:
    return _is_admin(p) or r.approver_id == p.user_id


def can_see_request(p: Principal, r: ShareRequest) -> bool:
    return can_decide(p, r) or r.requester_id == p.user_id


@dataclass
class InMemoryShareRequestStore:
    _requests: dict[str, ShareRequest] = field(default_factory=dict)

    async def create(self, agent: Agent, requester: Principal, approver_id: str, scope: SharedScope, comment: str) -> ShareRequest:
        r = ShareRequest(str(uuid.uuid4()), agent.agent_id, requester.user_id, approver_id, scope, comment)
        self._requests[r.request_id] = r
        return r

    async def get(self, request_id: str) -> ShareRequest | None:
        return self._requests.get(request_id)

    async def pending_for_agent(self, agent_id: str) -> ShareRequest | None:
        return next((r for r in self._requests.values() if r.agent_id == agent_id and r.status == "pending"), None)

    async def visible_to(self, p: Principal) -> list[ShareRequest]:
        items = [r for r in self._requests.values() if can_see_request(p, r)]
        return sorted(items, key=lambda r: r.requested_at, reverse=True)

    async def decide(self, r: ShareRequest, by: Principal, approved: bool, comment: str = "") -> ShareRequest:
        r = replace(
            r, status="approved" if approved else "rejected", decided_by=by.user_id,
            decision_comment=comment, decided_at=_now(),
        )
        self._requests[r.request_id] = r
        return r

    async def cancel_pending(self, agent_id: str, by: Principal, reason: str) -> None:
        """에이전트가 지워지거나 공유를 거두면 대기 중인 요청은 반려 처리한다."""
        r = await self.pending_for_agent(agent_id)
        if r is not None:
            await self.decide(r, by, approved=False, comment=reason)
