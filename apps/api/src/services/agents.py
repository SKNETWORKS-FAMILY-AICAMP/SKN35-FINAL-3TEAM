"""커스텀 에이전트 저장소와 조회·관리 권한.

권한 규칙 (2026-11-19 합의):
- 일반 사용자: 자기 에이전트 + 공유받은 에이전트(같은 부서의 dept 범위, company 범위)를 본다.
  관리는 자기 것만 한다.
- 부서장: 위에 더해 부서원이 만든 에이전트를 본다. 그 에이전트는 비활성화·재활성화만 할 수 있다.
- admin: 모든 에이전트를 보고 관리한다.
역할·부서는 서버가 채운 Principal 에서만 읽는다.

공유 범위(scope)는 바로 위 관리자의 승인으로만 바뀐다 (agent_share_request, 아직 미구현).
그래서 만들 때는 항상 personal 이고, 수정 API 로는 바꿀 수 없다.

지금은 메모리에 둔다. DB 가 붙으면 agent / agent_tool 테이블 구현으로 바꾼다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Literal

from contracts.permission import Principal

Scope = Literal["personal", "dept", "company"]
Status = Literal["active", "disabled"]

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
    """이름·설명·프롬프트·툴 수정, 삭제."""
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

    def create(
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

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def visible_to(self, p: Principal) -> list[Agent]:
        return [a for a in self._agents.values() if can_view(p, a)]

    def save(self, agent: Agent) -> Agent:
        self._agents[agent.agent_id] = agent
        return agent

    def update(self, agent: Agent, **changes: object) -> Agent:
        return self.save(replace(agent, **changes))

    def delete(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)
