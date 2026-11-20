"""채팅 1건 처리: 세션 기록을 붙여 하네스를 돌리고, 화면에 보여줄 툴 실행 내역을 만든다."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from contracts.harness import Denial, Message, RunResult
from contracts.permission import Principal
from harness import create_harness
from services.agents import Agent
from services.container import AppServices
from services.sessions import ChatSession


@dataclass(frozen=True)
class ToolEvent:
    """COM-03: 에이전트가 실행한 툴과 결과."""

    call_id: str
    tool_name: str
    display_name: str  # ToolSpec.label
    arguments: dict[str, Any]
    result: str
    ok: bool


@dataclass(frozen=True)
class Source:
    """COM-04: 이번 답에 쓰인 문서. 툴 결과에서 모은다 (LLM 이 근거를 빠뜨려도 화면에는 나온다)."""

    title: str
    location: str
    opened: bool  # True = 원문을 열어 읽음, False = 검색 결과에만 나옴


MAX_SOURCES = 5


def sources(new_messages: list[Message]) -> list[Source]:
    """문서 툴 결과(JSON)에서 문서명·위치를 모은다. 원문을 연 문서가 앞에 온다."""
    opened: dict[tuple[str, str], Source] = {}
    found: dict[tuple[str, str], Source] = {}
    for m in new_messages:
        if m.role != "tool" or m.content.startswith("error:"):
            continue
        try:
            data = json.loads(m.content)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        items = [(data, True)] if "title" in data else [(d, False) for d in data.get("matches", []) if isinstance(d, dict)]
        for item, was_opened in items:
            title, location = str(item.get("title") or ""), str(item.get("location") or "")
            if title:
                (opened if was_opened else found)[(title, location)] = Source(title, location, was_opened)
    merged = list(opened.values()) + [s for k, s in found.items() if k not in opened]
    return merged[:MAX_SOURCES]


@dataclass(frozen=True)
class ChatTurn:
    session_id: str
    run_id: str
    stop_reason: str
    message: str
    tool_events: list[ToolEvent]
    denial: Denial | None = None
    sources: list[Source] = ()


def tool_events(new_messages: list[Message], labels: dict[str, str]) -> list[ToolEvent]:
    results = {m.tool_call_id: m.content for m in new_messages if m.role == "tool"}
    events = []
    for m in new_messages:
        for tc in m.tool_calls:
            content = results.get(tc.call_id, "")
            label = labels.get(tc.tool_name, tc.tool_name)
            events.append(ToolEvent(tc.call_id, tc.tool_name, label, tc.arguments, content, not content.startswith("error:")))
    return events


def system_prompt(svc: AppServices, agent: Agent | None) -> str:
    """기본 규칙 + 에이전트 역할. 권한은 코드(ABAC)가 막으므로 프롬프트로 풀 수 없다."""
    if agent is None:
        return svc.system_prompt
    return f"{svc.system_prompt}\n\n[이 에이전트의 역할: {agent.name}]\n{agent.system_prompt}"


async def agent_tools(svc: AppServices, agent: Agent | None):
    """에이전트에 넣은 툴만 LLM 에게 보인다. 그사이 등록이 빠진 툴은 건너뛴다."""
    if agent is None:
        return svc.tools
    return svc.tools.only([n for n in agent.tools if await svc.tools.get(n) is not None])


async def send_message(
    svc: AppServices, principal: Principal, session: ChatSession, content: str, agent: Agent | None = None,
) -> ChatTurn:
    tools = await agent_tools(svc, agent)
    harness = create_harness(
        llm=svc.llm, tools=tools, checker=svc.checker, approver=svc.approvals,
        audit=svc.audit, max_turns=svc.max_turns, tool_timeout=svc.tool_timeout,
    )
    run_id = str(uuid.uuid4())
    async with session.lock:
        # 시스템 프롬프트는 저장하지 않고 매번 붙인다 (에이전트 프롬프트를 고치면 다음 요청부터 반영)
        history = [Message(role="system", content=system_prompt(svc, agent)), *session.messages]
        result: RunResult = await harness.run(
            content, principal, session.session_id, history,
            agent_id=session.agent_id, run_id=run_id,
        )
        new = result.messages[len(history):]
        session.messages = list(result.messages[1:])

    if result.final_message is not None:
        text = result.final_message.content
    else:
        text = "요청을 처리하는 데 단계가 너무 많아 중단했습니다. 요청을 나눠서 다시 시도해 주세요."
    labels = {s.name: s.label for s in await tools.specs()}
    return ChatTurn(
        session.session_id, run_id, result.stop_reason, text, tool_events(new, labels), result.denial, sources(new),
    )
