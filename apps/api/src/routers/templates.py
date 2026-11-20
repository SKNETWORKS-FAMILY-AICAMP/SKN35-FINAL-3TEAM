"""에이전트 템플릿: 누구나 보고 골라 쓸 수 있고, 만들고 고치고 지우는 건 관리자만 한다."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from contracts.permission import Principal
from routers.agents import MAX_TOOLS, check_tools
from routers.deps import current_user, get_services
from services.container import AppServices
from services.templates import AgentTemplate, can_manage_templates

router = APIRouter(tags=["templates"])


class TemplateIn(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{
        "name": "규정 안내",
        "description": "내규를 찾아 근거와 함께 답한다",
        "system_prompt": "직원이 묻는 규정을 사내 문서에서 찾아 조문에 적힌 내용만으로 답한다.",
        "tools": ["search_doc", "get_doc_chunk"],
        "example": "서류 반려 사유는 어떻게 적어?",
    }]}}

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    system_prompt: str = Field(min_length=1, max_length=4000)
    tools: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)
    example: str = Field(default="", max_length=500)


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    tools: list[str] | None = Field(default=None, max_length=MAX_TOOLS)
    example: str | None = Field(default=None, max_length=500)


class TemplateOut(BaseModel):
    template_id: str
    name: str
    description: str
    system_prompt: str
    tools: list[str]
    example: str
    created_by: str
    updated_at: datetime | None
    missing_tools: list[str]  # 아직 등록되지 않은 툴. 있으면 '준비 중'
    available: bool  # 지금 이 템플릿으로 에이전트를 만들 수 있는지


async def to_out(t: AgentTemplate, svc: AppServices) -> TemplateOut:
    missing = [n for n in t.tools if await svc.tools.get(n) is None]
    return TemplateOut(
        template_id=t.template_id, name=t.name, description=t.description, system_prompt=t.system_prompt,
        tools=list(t.tools), example=t.example, created_by=t.created_by, updated_at=t.updated_at,
        missing_tools=missing, available=not missing,
    )


def require_admin(user: Principal) -> None:
    if not can_manage_templates(user):
        raise HTTPException(403, "템플릿은 관리자만 만들고 고칠 수 있습니다")


def load(svc: AppServices, template_id: str) -> AgentTemplate:
    t = svc.templates.get(template_id)
    if t is None:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    return t


@router.get("/api/templates", response_model=list[TemplateOut])
async def list_templates(
    _: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> list[TemplateOut]:
    return [await to_out(t, svc) for t in svc.templates.list()]


@router.post("/api/templates", response_model=TemplateOut, status_code=201)
async def create_template(
    body: TemplateIn, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> TemplateOut:
    require_admin(user)
    tools = await check_tools(svc, body.tools)  # 화면에서 만드는 템플릿은 등록된 툴로만
    t = svc.templates.create(user, **body.model_dump(exclude={"tools"}), tools=tools)
    return await to_out(t, svc)


@router.patch("/api/templates/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: str, body: TemplatePatch,
    user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> TemplateOut:
    require_admin(user)
    current = load(svc, template_id)
    fields = body.model_dump(exclude_none=True)
    if "tools" in fields:
        # 기본 템플릿에 원래 있던 '준비 중' 툴은 그대로 둘 수 있다. 새로 넣는 툴만 등록 여부를 본다.
        added = [n for n in fields["tools"] if n not in current.tools]
        await check_tools(svc, added)
        if len(set(fields["tools"])) != len(fields["tools"]):
            raise HTTPException(422, "같은 툴을 두 번 넣을 수 없습니다")
        fields["tools"] = tuple(fields["tools"])
    return await to_out(svc.templates.update(template_id, **fields), svc)


@router.delete("/api/templates/{template_id}", status_code=204)
async def delete_template(
    template_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services),
) -> None:
    require_admin(user)
    load(svc, template_id)
    svc.templates.delete(template_id)
