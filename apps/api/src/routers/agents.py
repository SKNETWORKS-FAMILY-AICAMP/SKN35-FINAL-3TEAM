"""커스텀 에이전트 만들기·조회·관리, 에이전트에 넣을 수 있는 툴 목록.

볼 수 없는 에이전트는 404 (있는지도 알려주지 않는다), 볼 수는 있지만 권한이 없으면 403.
권한 규칙은 services/agents.py 참고.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services import agents as rules
from services.agents import Agent
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
    can_edit: bool
    can_disable: bool
    can_enable: bool


def to_out(a: Agent, p: Principal) -> AgentOut:
    return AgentOut(
        agent_id=a.agent_id, owner_user_id=a.owner_user_id, name=a.name, description=a.description,
        system_prompt=a.system_prompt, tools=list(a.tools), scope=a.scope, status=a.status,
        can_edit=rules.can_edit(p, a),
        can_disable=a.status == "active" and rules.can_disable(p, a),
        can_enable=a.status == "disabled" and rules.can_enable(p, a),
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
    return to_out(a, user)


@router.get("/api/agents", response_model=list[AgentOut])
async def list_agents(
    mine: bool = False, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> list[AgentOut]:
    """내가 볼 수 있는 에이전트. mine=true 면 내가 만든 것만."""
    agents = svc.agents.visible_to(user)
    if mine:
        agents = [a for a in agents if a.owner_user_id == user.user_id]
    return [to_out(a, user) for a in agents]


@router.get("/api/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    return to_out(load(svc, agent_id, user, rules.can_view), user)


@router.patch("/api/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str, body: AgentPatch,
    user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    a = load(svc, agent_id, user, rules.can_edit)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "tools" in changes:
        changes["tools"] = await check_tools(svc, changes["tools"])
    return to_out(svc.agents.update(a, **changes), user)


@router.post("/api/agents/{agent_id}/disable", response_model=AgentOut)
async def disable_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    a = load(svc, agent_id, user, rules.can_disable)
    return to_out(svc.agents.update(a, status="disabled", disabled_by=user.user_id), user)


@router.post("/api/agents/{agent_id}/enable", response_model=AgentOut)
async def enable_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> AgentOut:
    a = load(svc, agent_id, user, rules.can_enable)
    return to_out(svc.agents.update(a, status="active", disabled_by=None), user)


@router.delete("/api/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> None:
    load(svc, agent_id, user, rules.can_edit)
    svc.agents.delete(agent_id)
