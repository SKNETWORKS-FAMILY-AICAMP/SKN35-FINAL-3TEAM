"""자연어 설명으로 에이전트 초안 만들기.

LLM 이 이름·설명·시스템 프롬프트·툴 선택을 JSON 으로 만든다. 초안은 저장하지 않고 돌려만 준다.
사용자가 화면에서 고친 뒤 POST /api/agents 로 저장한다.

- 툴은 등록된 것 중에서만 남긴다 (LLM 이 지어낸 이름은 버린다).
- 조회(read) 툴만 바로 넣는다. 등록·변경(write)·발송(send) 툴은 suggested_tools 로 추천만 하고,
  사용자가 화면에서 직접 체크해야 들어간다 (작은 모델이 비슷한 툴을 잘못 고르는 일이 있어서).
- 역할 지시에 툴 영문 이름이 남아 있으면 화면 이름으로 바꾼다 (모델이 규칙을 자주 어긴다).
- 필요하지만 등록된 툴로는 못 하는 일은 missing 으로 따로 돌려준다 (화면에 보여 주고 툴 요청 근거로 쓴다).
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
EFFECT_LABEL = {"read": "조회", "write": "등록·변경", "send": "발송"}
AUTO_EFFECTS = {"read"}  # 초안에 바로 체크해 넣는 툴 종류

SYSTEM = """너는 사내 업무 에이전트의 설정 초안을 만드는 도우미다.
사용자가 원하는 에이전트를 설명하면, 아래 형식의 JSON 객체 하나만 출력한다. 다른 글은 쓰지 않는다.

{{"name": "짧은 이름 (20자 이내)",
  "description": "이 에이전트가 하는 일 한 문장",
  "system_prompt": "에이전트에게 줄 역할 지시. 누구를 돕는지, 해야 할 일, 하지 말아야 할 일을 한국어로 3~5문장",
  "tools": ["필요한 툴 이름"],
  "missing": ["필요하지만 아래 툴로는 할 수 없는 일"]}}

규칙:
- tools 에는 아래 목록에 있는 name 만 넣는다. 필요 없으면 빈 목록. 툴 선택은 tools 로만 한다.
- 툴은 설명에 적힌 일이 사용자가 원하는 일과 정확히 같을 때만 고른다. 이름이나 단어가 비슷하다고 다른 일에 갖다 쓰지 않는다.
  예) "고객 안내 메일 발송"은 "심사 요청 등록"과 다른 일이다. 환율·금리 같은 수치는 문서 검색으로 대신하지 않는다.
- 필요한데 목록에 없는 기능은 missing 에 "만기 대출 조회"처럼 짧게 적는다. 모두 있으면 빈 목록.
- system_prompt 에는 툴 이름(영문 name), ID, JSON 을 쓰지 않는다. "사내 문서를 찾아서"처럼 하는 일을 쉬운 말로 쓴다.
- 답 형식(결론 먼저, 근거 표시, 내부 값 숨기기)은 공통 규칙이 정하므로 system_prompt 에 따로 쓰지 않는다.
- missing 에 적은 일은 system_prompt 에 "그 일은 아직 할 수 없다고 안내한다"고 한 문장으로만 쓴다.
- 툴이 어떻게 동작하는지(휴일 반영 여부 등)는 툴 설명에 적힌 것만 쓰고 짐작해서 쓰지 않는다.
- 사용자가 말하지 않은 조건·제외·금지 규칙을 지어 넣지 않는다.
  예) "요약이 없으면 보내지 않는다", "만기가 지난 건은 빼라" 처럼 설명에 없는 제한을 만들지 않는다.
  툴 인자 중 무엇이 필수인지는 툴이 정하므로 system_prompt 에 적지 않는다.
- system_prompt 에 권한을 무시하거나 우회하라는 내용을 넣지 않는다.

사용할 수 있는 툴:
{tools}"""

# 다시 물을 때는 생각(추론) 없이 바로 쓰게 한다 (/no_think, qwen3). 14b 의 기본 한도(4,096)에서는 답 몫이
# 1,024 토큰뿐이라, 생각이 길면 JSON 을 쓰기 전에 끊긴다 (2026-11-24 실측: 초안이 기본 초안으로 떨어져 툴 0개)
RETRY = "방금 답은 올바른 JSON 이 아니었거나 끊겼다. 생각을 쓰지 말고 위 형식의 JSON 객체 하나만 바로 출력해라. /no_think"


@dataclass(frozen=True)
class AgentDraft:
    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    source: Literal["llm", "fallback"] = "llm"  # fallback = LLM 답을 못 써서 키워드로 만든 초안
    dropped_tools: list[str] = field(default_factory=list)  # LLM 이 골랐지만 등록되지 않아 뺀 툴
    suggested_tools: list[str] = field(default_factory=list)  # LLM 이 고른 등록·발송 툴. 사용자가 직접 체크해야 들어간다
    missing: list[str] = field(default_factory=list)  # 필요하지만 등록된 툴로는 못 하는 일


def _tool_lines(specs: list[ToolSpec]) -> str:
    return "\n".join(f"- {s.name} ({s.label}, {EFFECT_LABEL.get(s.effect, s.effect)}): {s.description}" for s in specs) or "- (없음)"


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


def humanize_tool_names(text: str, specs: list[ToolSpec]) -> str:
    """역할 지시 안의 툴 영문 이름(따옴표·백틱 포함)을 화면 이름으로 바꾼다. 긴 이름부터 바꿔 겹침을 막는다."""
    for s in sorted(specs, key=lambda s: len(s.name), reverse=True):
        if s.display_name:
            text = re.sub(rf"[`'\"]?(?<![A-Za-z0-9_]){re.escape(s.name)}(?![A-Za-z0-9_])[`'\"]?", f"'{s.display_name}'", text)
    return re.sub(r"(?<![A-Za-z0-9_])chunk_id(?![A-Za-z0-9_])", "문단 번호", text)


# 사용자가 말하지 않았는데 모델이 자주 지어 넣는 제한 문장 (실측: "만기 경과 건은 포함하지 않는다", "요약문이 없으면 보내지 않는다")
_EXCLUSION = re.compile(r"포함하지\s*않|제외|빼고|빼며|뺀다|빼야")
_CONDITIONAL_BAN = re.compile(r"없으면[^.!?\n]*(?:않|말|안\s)")


def drop_invented_rules(prompt: str, description: str) -> str:
    """역할 지시에서 사용자 설명에 없는 제외·조건부 금지 문장을 뺀다. 사용자가 그런 말을 했으면 그대로 둔다.
    "아직 할 수 없다고 안내한다" 같은 빈 안내 문장도 뺀다 (missing 이 비었는데 붙는 일이 있음)."""
    keep = []
    for sentence in re.split(r"(?<=[.!?다])\s+", prompt.strip()):
        if _EXCLUSION.search(sentence) and not _EXCLUSION.search(description):
            continue
        if _CONDITIONAL_BAN.search(sentence) and "없으면" not in description:
            continue
        if re.fullmatch(r"\W*그 일은 아직 할 수 없다고 안내한다\W*", sentence):
            continue
        keep.append(sentence)
    return " ".join(keep).strip()


# 설명에 파일·첨부 이야기가 있으면 '업로드 문서 읽기'(parse_doc)를 같이 넣는다 (읽기 툴이라 바로 체크).
# 모델이 자주 빠뜨린다: "엑셀로 받은 목록", "신청서를 보고" 같은 설명에서도 첨부를 읽어야 한다.
_ATTACHMENT_WORDS = re.compile(r"첨부|업로드|파일|엑셀|xlsx|pdf|hwp|docx|서류|신청서|명세서|목록을\s*보|자료를\s*보", re.I)


def with_attachment_reader(tools: list[str], description: str, specs: list[ToolSpec]) -> list[str]:
    if "parse_doc" in tools or not any(s.name == "parse_doc" for s in specs) or not _ATTACHMENT_WORDS.search(description):
        return tools
    return [*tools, "parse_doc"]


def _strings(value: Any, limit: int = 8) -> list[str]:
    items = value if isinstance(value, list) else []
    return list(dict.fromkeys(str(v).strip()[:60] for v in items if str(v).strip()))[:limit]


def _clean(data: dict[str, Any], specs: list[ToolSpec], description: str) -> AgentDraft | None:
    name = str(data.get("name") or "").strip()
    prompt = str(data.get("system_prompt") or "").strip()
    if not name or not prompt:
        return None
    effects = {s.name: s.effect for s in specs}
    picked = data.get("tools") if isinstance(data.get("tools"), list) else []
    picked = list(dict.fromkeys(str(t) for t in picked))  # 중복 제거, 순서 유지
    return AgentDraft(
        name=name[:MAX_NAME],
        description=(str(data.get("description") or "").strip() or description)[:MAX_DESCRIPTION],
        system_prompt=(drop_invented_rules(humanize_tool_names(prompt, specs), description) or prompt)[:4000],
        tools=with_attachment_reader([t for t in picked if effects.get(t) in AUTO_EFFECTS], description, specs),
        suggested_tools=[t for t in picked if t in effects and effects[t] not in AUTO_EFFECTS],
        dropped_tools=[t for t in picked if t not in effects],
        missing=_strings(data.get("missing")),
    )


def fallback_draft(description: str, specs: list[ToolSpec]) -> AgentDraft:
    """LLM 없이 만드는 기본 초안: 설명 첫 문장을 이름으로, 이름·표시 이름이 언급된 툴을 고른다."""
    text = description.strip()
    title = re.split(r"[.!?\n]", text)[0]
    title = re.sub(r"\s*(에이전트(?:를)?\s*)?(만들어\s*줘|만들어\s*주세요)\s*$", "", title).strip()
    mentioned = [s for s in specs if s.name in text or (s.display_name and s.display_name in text)]
    tools = with_attachment_reader([s.name for s in mentioned if s.effect in AUTO_EFFECTS], text, specs)
    suggested = [s.name for s in mentioned if s.effect not in AUTO_EFFECTS]
    prompt = (
        f"다음 역할에 따라 사용자의 요청을 처리한다.\n\n{text}\n\n"
        "정보가 부족하면 확인 질문을 하고, 결과는 한국어로 간결하게 정리한다."
    )
    return AgentDraft(
        (title or "새 에이전트")[:MAX_NAME], text[:MAX_DESCRIPTION], prompt, tools, "fallback", suggested_tools=suggested,
    )


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
