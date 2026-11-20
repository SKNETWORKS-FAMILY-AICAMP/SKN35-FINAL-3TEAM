"""커스텀 에이전트 만들기·조회·관리, 에이전트에 넣을 수 있는 툴 목록.

볼 수 없는 에이전트는 404 (있는지도 알려주지 않는다), 볼 수는 있지만 권한이 없으면 403.
권한 규칙은 services/agents.py 참고.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services import agents as rules
from services.agents import Agent, ShareRequest
from services.container import AppServices

router = APIRouter(tags=["agents"])

MAX_TOOLS = 20


class ToolOut(BaseModel):
    name: str
    display_name: str
    description: str


class AgentIn(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{
        "name": "계산 도우미",
        "description": "덧셈·곱셈을 대신 해 준다",
        "system_prompt": "숫자 계산은 반드시 툴로 하고, 결과만 짧게 답한다.",
        "tools": ["add", "multiply"],
    }]}}

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    system_prompt: str = Field(min_length=1, max_length=4000)
    tools: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)


class AgentPatch(BaseModel):
    """바꿀 항목만 보낸다. 공유 범위(scope)는 승인 절차로만 바뀐다."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    tools: list[str] | None = Field(default=None, max_length=MAX_TOOLS)


class AgentOut(BaseModel):
    agent_id: str
    owner_user_id: str
    name: str
    description: str
    system_prompt: str
    tools: list[str]
    scope: str
    status: str
    # 화면에서 버튼을 보여줄지 정하는 값 (실제 검사는 서버가 다시 한다)
    can_edit: bool  # 이름·설명·프롬프트·툴 수정 (공유 중·승인 대기 중이면 false)
    can_delete: bool
    can_disable: bool
    can_enable: bool
    can_request_share: bool
    can_unshare: bool
    pending_share_scope: str | None = None  # 승인 대기 중인 공유 요청이 있으면 그 범위


def edit_lock(a: Agent, svc: AppServices) -> str | None:
    """공유된 에이전트는 승인받은 그대로 써야 하므로 편집을 막는다. 고치려면 공유를 해제한다."""
    if a.scope != "personal":
        return "공유 중인 에이전트는 수정할 수 없습니다. 공유를 해제한 뒤 수정하고 다시 공유 요청하세요"
    if svc.share_requests.pending_for_agent(a.agent_id) is not None:
        return "공유 승인 대기 중인 에이전트는 수정할 수 없습니다. 요청이 처리된 뒤 수정하세요"
    return None


def to_out(a: Agent, p: Principal, svc: AppServices) -> AgentOut:
    pending = svc.share_requests.pending_for_agent(a.agent_id)
    widest = a.scope == "company"
    return AgentOut(
        agent_id=a.agent_id, owner_user_id=a.owner_user_id, name=a.name, description=a.description,
        system_prompt=a.system_prompt, tools=list(a.tools), scope=a.scope, status=a.status,
        can_edit=rules.can_edit(p, a) and edit_lock(a, svc) is None,
        can_delete=rules.can_edit(p, a),
        can_disable=a.status == "active" and rules.can_disable(p, a),
        can_enable=a.status == "disabled" and rules.can_enable(p, a),
        can_request_share=(a.status == "active" and not widest and pending is None
                           and rules.can_request_share(p, a)),
        can_unshare=a.scope != "personal" and rules.can_unshare(p, a),
        pending_share_scope=pending.requested_scope if pending else None,
    )


async def check_tools(svc: AppServices, names: list[str]) -> tuple[str, ...]:
    """관리자가 등록해 둔 툴 중에서만 고를 수 있다."""
    if len(set(names)) != len(names):
        raise HTTPException(422, "같은 툴을 두 번 넣을 수 없습니다")
    unknown = [n for n in names if await svc.tools.get(n) is None]
    if unknown:
        raise HTTPException(422, f"등록되지 않은 툴입니다: {', '.join(unknown)}")
    return tuple(names)


def load(svc: AppServices, agent_id: str, user: Principal, allowed: Callable[[Principal, Agent], bool]) -> Agent:
    a = svc.agents.get(agent_id)
    if a is None or not rules.can_view(user, a):
        raise HTTPException(404, "에이전트를 찾을 수 없습니다")
    if not allowed(user, a):
        raise HTTPException(403, "이 에이전트에 대한 권한이 없습니다")
    return a


@router.get("/api/tools", response_model=list[ToolOut])
async def list_tools(
    _: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> list[ToolOut]:
    """에이전트를 만들 때 고를 수 있는 툴. 실제 실행 가능 여부는 실행 때 권한 정책이 다시 판정한다."""
    return [ToolOut(name=s.name, display_name=s.label, description=s.description) for s in await svc.tools.specs()]


@router.post("/api/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    body: AgentIn, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    tools = await check_tools(svc, body.tools)
    a = svc.agents.create(
        user, name=body.name, system_prompt=body.system_prompt, tools=tools, description=body.description,
    )
    return to_out(a, user, svc)


@router.get("/api/agents", response_model=list[AgentOut])
async def list_agents(
    mine: bool = False, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> list[AgentOut]:
    """내가 볼 수 있는 에이전트. mine=true 면 내가 만든 것만."""
    agents = svc.agents.visible_to(user)
    if mine:
        agents = [a for a in agents if a.owner_user_id == user.user_id]
    return [to_out(a, user, svc) for a in agents]


@router.get("/api/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    return to_out(load(svc, agent_id, user, rules.can_view), user, svc)


@router.patch("/api/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str, body: AgentPatch,
    user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    a = load(svc, agent_id, user, rules.can_edit)
    if (lock := edit_lock(a, svc)) is not None:
        raise HTTPException(409, lock)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "tools" in changes:
        changes["tools"] = await check_tools(svc, changes["tools"])
    return to_out(svc.agents.update(a, **changes), user, svc)


@router.post("/api/agents/{agent_id}/disable", response_model=AgentOut)
async def disable_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    a = load(svc, agent_id, user, rules.can_disable)
    return to_out(svc.agents.update(a, status="disabled", disabled_by=user.user_id), user, svc)


@router.post("/api/agents/{agent_id}/enable", response_model=AgentOut)
async def enable_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    a = load(svc, agent_id, user, rules.can_enable)
    return to_out(svc.agents.update(a, status="active", disabled_by=None), user, svc)


@router.delete("/api/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> None:
    load(svc, agent_id, user, rules.can_edit)
    svc.share_requests.cancel_pending(agent_id, user, "에이전트가 삭제됨")
    svc.agents.delete(agent_id)


# ── 공유 요청 ──────────────────────────────────────────────

class ShareRequestIn(BaseModel):
    scope: Literal["dept", "company"]
    comment: str = Field(default="", max_length=1000)  # 요청 사유


class DecisionIn(BaseModel):
    comment: str = Field(default="", max_length=1000)


class ShareRequestOut(BaseModel):
    request_id: str
    agent_id: str
    agent_name: str
    requester_id: str
    approver_id: str
    requested_scope: str
    comment: str
    status: str  # pending | approved | rejected
    decided_by: str | None
    decision_comment: str
    requested_at: datetime
    decided_at: datetime | None
    can_decide: bool


def request_out(r: ShareRequest, p: Principal, svc: AppServices) -> ShareRequestOut:
    a = svc.agents.get(r.agent_id)
    return ShareRequestOut(
        **{k: getattr(r, k) for k in (
            "request_id", "agent_id", "requester_id", "approver_id", "requested_scope", "comment",
            "status", "decided_by", "decision_comment", "requested_at", "decided_at",
        )},
        agent_name=a.name if a else "(삭제된 에이전트)",
        can_decide=r.status == "pending" and rules.can_decide(p, r),
    )


@router.post("/api/agents/{agent_id}/share-requests", response_model=ShareRequestOut, status_code=201)
async def request_share(
    agent_id: str, body: ShareRequestIn,
    user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> ShareRequestOut:
    """공유 범위를 넓혀 달라고 바로 위 관리자에게 요청한다. admin 은 바로 적용된다."""
    a = load(svc, agent_id, user, rules.can_request_share)
    if a.status != "active":
        raise HTTPException(409, "비활성화된 에이전트는 공유할 수 없습니다")
    if rules.SCOPE_ORDER[body.scope] <= rules.SCOPE_ORDER[a.scope]:
        raise HTTPException(409, f"이미 '{a.scope}' 범위로 공유되어 있습니다")
    if svc.share_requests.pending_for_agent(a.agent_id) is not None:
        raise HTTPException(409, "이미 승인 대기 중인 공유 요청이 있습니다")

    superior = svc.users.superior_of(user)
    if superior is None:  # admin: 위가 없으니 바로 적용 (기록은 남긴다)
        r = svc.share_requests.create(a, user, user.user_id, body.scope, body.comment)
        r = svc.share_requests.decide(r, user, approved=True, comment="관리자 직접 적용")
        svc.agents.update(a, scope=body.scope)
    else:
        r = svc.share_requests.create(a, user, superior.user_id, body.scope, body.comment)
    return request_out(r, user, svc)


@router.get("/api/share-requests", response_model=list[ShareRequestOut])
async def list_share_requests(
    box: Literal["inbox", "outbox", "all"] = "all",
    user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> list[ShareRequestOut]:
    """inbox: 내가 결정할 대기 요청 / outbox: 내가 보낸 요청 / all: 볼 수 있는 전부 (최근 순)."""
    items = svc.share_requests.visible_to(user)
    if box == "inbox":
        items = [r for r in items if r.status == "pending" and rules.can_decide(user, r)]
    elif box == "outbox":
        items = [r for r in items if r.requester_id == user.user_id]
    return [request_out(r, user, svc) for r in items]


def _decide(svc: AppServices, request_id: str, user: Principal, approved: bool, comment: str) -> ShareRequestOut:
    r = svc.share_requests.get(request_id)
    if r is None or not rules.can_see_request(user, r):
        raise HTTPException(404, "공유 요청을 찾을 수 없습니다")
    if not rules.can_decide(user, r):
        raise HTTPException(403, "이 요청을 결정할 권한이 없습니다")
    if r.status != "pending":
        raise HTTPException(409, "이미 처리된 요청입니다")
    a = svc.agents.get(r.agent_id)
    if approved:
        if a is None or a.status != "active":
            raise HTTPException(409, "에이전트가 삭제됐거나 비활성화되어 승인할 수 없습니다")
        if rules.SCOPE_ORDER[r.requested_scope] > rules.SCOPE_ORDER[a.scope]:
            svc.agents.update(a, scope=r.requested_scope)
    return request_out(svc.share_requests.decide(r, user, approved, comment), user, svc)


@router.post("/api/share-requests/{request_id}/approve", response_model=ShareRequestOut)
async def approve_share(
    request_id: str, body: DecisionIn | None = None,
    user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> ShareRequestOut:
    return _decide(svc, request_id, user, True, body.comment if body else "")


@router.post("/api/share-requests/{request_id}/reject", response_model=ShareRequestOut)
async def reject_share(
    request_id: str, body: DecisionIn | None = None,
    user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> ShareRequestOut:
    return _decide(svc, request_id, user, False, body.comment if body else "")


@router.post("/api/agents/{agent_id}/unshare", response_model=AgentOut)
async def unshare_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    """공유를 거두고 개인용으로 되돌린다. 승인 없이 주인·admin 이 바로 한다."""
    a = load(svc, agent_id, user, rules.can_unshare)
    svc.share_requests.cancel_pending(a.agent_id, user, "공유를 거둠")
    return to_out(svc.agents.update(a, scope="personal"), user, svc)
