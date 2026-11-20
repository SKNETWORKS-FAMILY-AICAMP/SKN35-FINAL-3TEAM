"""관리자가 만든 에이전트 템플릿 (시나리오 템플릿).

은행이 미리 정한 핵심 업무(UC-01~05)는 LLM 즉석 초안 대신 관리자가 검증한 템플릿으로 시작한다.
직원은 템플릿을 골라 내 에이전트로 저장하고(그다음은 일반 에이전트와 같다), 템플릿 자체는 관리자만 고친다.

- 기본 템플릿은 config/agent_templates.yaml 에서 읽는다 (DB 가 붙으면 테이블로 옮긴다).
- 템플릿의 툴이 아직 등록되지 않았으면 '준비 중'으로 보여 주고 사용할 수 없다.
- 지금은 메모리에만 있어서 서버를 다시 켜면 화면에서 고친 내용은 사라진다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from contracts.permission import Principal


@dataclass(frozen=True)
class AgentTemplate:
    template_id: str
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] = ()
    example: str = ""  # 이 템플릿으로 만든 에이전트에게 보낼 요청 예시
    created_by: str = "admin"
    updated_at: datetime | None = None


def can_manage_templates(p: Principal) -> bool:
    return "admin" in p.roles


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_templates(path: str | Path) -> list[AgentTemplate]:
    file = Path(path)
    if not file.exists():
        return []
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    out = []
    for raw in data.get("templates", []):
        out.append(AgentTemplate(
            template_id=str(raw["id"]),
            name=str(raw["name"]),
            description=str(raw.get("description", "")),
            system_prompt=str(raw["system_prompt"]).strip(),
            tools=tuple(raw.get("tools", ())),
            example=str(raw.get("example", "")),
            created_by=str(raw.get("created_by", "admin")),
            updated_at=_now(),
        ))
    return out


class InMemoryTemplateStore:
    def __init__(self, seed: list[AgentTemplate] | None = None) -> None:
        self._items: dict[str, AgentTemplate] = {t.template_id: t for t in seed or ()}

    def list(self) -> list[AgentTemplate]:
        return list(self._items.values())

    def get(self, template_id: str) -> AgentTemplate | None:
        return self._items.get(template_id)

    def create(self, by: Principal, **fields: Any) -> AgentTemplate:
        t = AgentTemplate(template_id=str(uuid.uuid4()), created_by=by.user_id, updated_at=_now(), **fields)
        self._items[t.template_id] = t
        return t

    def update(self, template_id: str, **fields: Any) -> AgentTemplate:
        t = replace(self._items[template_id], updated_at=_now(), **fields)
        self._items[template_id] = t
        return t

    def delete(self, template_id: str) -> None:
        self._items.pop(template_id, None)
