"""자연어 설명으로 에이전트 초안 만들기.

LLM 이 이름·설명·시스템 프롬프트·툴 선택을 JSON 으로 만든다. 초안은 저장하지 않고 돌려만 준다.
사용자가 화면에서 고친 뒤 POST /api/agents 로 저장한다.

- 툴은 등록된 것 중에서만 남긴다 (LLM 이 지어낸 이름은 버린다).
- 툴을 고르는 것은 권한과 별개다. 실행할 때 ABAC 가 다시 판정한다.
- JSON 이 깨지면 한 번 더 요청하고, 그래도 안 되면 키워드로 만든 기본 초안을 돌려준다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from contracts.harness import LLMClient, Message, ToolSpec

log = logging.getLogger(__name__)

MAX_NAME = 40
MAX_DESCRIPTION = 300

SYSTEM = """너는 사내 업무 에이전트의 설정 초안을 만드는 도우미다.
사용자가 원하는 에이전트를 설명하면, 아래 형식의 JSON 객체 하나만 출력한다. 다른 글은 쓰지 않는다.

{{"name": "짧은 이름 (20자 이내)",
  "description": "이 에이전트가 하는 일 한 문장",
  "system_prompt": "에이전트에게 줄 역할 지시. 누구를 돕는지, 해야 할 일, 하지 말아야 할 일을 한국어로 3~5문장",
  "tools": ["필요한 툴 이름"]}}

규칙:
- tools 에는 아래 목록에 있는 name 만 넣는다. 필요 없으면 빈 목록. 툴 선택은 tools 로만 한다.
- system_prompt 에는 툴 이름(영문 name), ID, JSON 을 쓰지 않는다. "사내 문서를 찾아서"처럼 하는 일을 쉬운 말로 쓴다.
- 답 형식(결론 먼저, 근거 표시, 내부 값 숨기기)은 공통 규칙이 정하므로 system_prompt 에 따로 쓰지 않는다.
- 목록에 없는 기능이 필요하면 툴을 지어내지 말고 system_prompt 에 "그 기능은 할 수 없다고 안내한다"고 쓴다.
- system_prompt 에 권한을 무시하거나 우회하라는 내용을 넣지 않는다.

사용할 수 있는 툴:
{tools}"""

RETRY = "방금 답은 올바른 JSON 이 아니었다. 설명 없이 위 형식의 JSON 객체 하나만 다시 출력해라."


@dataclass(frozen=True)
class AgentDraft:
    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    source: Literal["llm", "fallback"] = "llm"  # fallback = LLM 답을 못 써서 키워드로 만든 초안
    dropped_tools: list[str] = field(default_factory=list)  # LLM 이 골랐지만 등록되지 않아 뺀 툴


def _tool_lines(specs: list[ToolSpec]) -> str:
    return "\n".join(f"- {s.name} ({s.label}): {s.description}" for s in specs) or "- (없음)"


def parse_json_object(text: str) -> dict[str, Any] | None:
    """모델 답에서 JSON 객체를 꺼낸다. 코드 블록·앞뒤 설명이 섞여 있어도 첫 { 부터 마지막 } 까지 본다."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clean(data: dict[str, Any], specs: list[ToolSpec], description: str) -> AgentDraft | None:
    name = str(data.get("name") or "").strip()
    prompt = str(data.get("system_prompt") or "").strip()
    if not name or not prompt:
        return None
    known = {s.name for s in specs}
    picked = data.get("tools") if isinstance(data.get("tools"), list) else []
    picked = list(dict.fromkeys(str(t) for t in picked))  # 중복 제거, 순서 유지
    return AgentDraft(
        name=name[:MAX_NAME],
        description=(str(data.get("description") or "").strip() or description)[:MAX_DESCRIPTION],
        system_prompt=prompt[:4000],
        tools=[t for t in picked if t in known],
        dropped_tools=[t for t in picked if t not in known],
    )


def fallback_draft(description: str, specs: list[ToolSpec]) -> AgentDraft:
    """LLM 없이 만드는 기본 초안: 설명 첫 문장을 이름으로, 이름·표시 이름이 언급된 툴을 고른다."""
    text = description.strip()
    title = re.split(r"[.!?\n]", text)[0]
    title = re.sub(r"\s*(에이전트(?:를)?\s*)?(만들어\s*줘|만들어\s*주세요)\s*$", "", title).strip()
    tools = [s.name for s in specs if s.name in text or (s.display_name and s.display_name in text)]
    prompt = (
        f"다음 역할에 따라 사용자의 요청을 처리한다.\n\n{text}\n\n"
        "정보가 부족하면 확인 질문을 하고, 결과는 한국어로 간결하게 정리한다."
    )
    return AgentDraft((title or "새 에이전트")[:MAX_NAME], text[:MAX_DESCRIPTION], prompt, tools, "fallback")


async def draft_agent(llm: LLMClient, description: str, specs: list[ToolSpec]) -> AgentDraft:
    """LLM 호출 실패(서버 다운 등)는 그대로 예외로 올린다. 답 형식이 틀린 것만 기본 초안으로 대신한다."""
    messages = [Message("system", SYSTEM.format(tools=_tool_lines(specs))), Message("user", description)]
    for attempt in range(2):
        reply = (await llm.chat(messages, [])).message.content
        data = parse_json_object(reply)
        draft = _clean(data, specs, description) if data else None
        if draft is not None:
            return draft
        log.info("에이전트 초안 JSON 파싱 실패 (%d회차): %.200s", attempt + 1, reply)
        messages = [*messages, Message("assistant", reply), Message("user", RETRY)]
    return fallback_draft(description, specs)
