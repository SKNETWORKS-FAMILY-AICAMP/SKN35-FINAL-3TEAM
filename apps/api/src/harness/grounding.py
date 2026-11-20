"""문서 질문이면 답하기 전에 사내 문서 검색을 강제한다 (지어낸 규정·근거 막기).

작은 모델은 "규정을 찾아보고 답하라"는 프롬프트를 자주 무시하고, 검색 없이 그럴듯한 조항까지 지어낸다.
(qwen3 8b·14b 모두 확인). 그래서 하네스가 최종 답을 받기 전에 한 번 검사한다.

언제 강제하나 (둘 다 이번 run 에서 문서 툴을 한 번도 성공하지 못했을 때만):
- 답에 문서 근거를 단 경우: "근거:" 줄이나 "제3절 2항" 같은 조항 표시가 있다
- 질문이 규정을 묻는 경우: 규정·지침·절차·누가 승인 같은 말이 있다
그리고 에이전트에 검색 툴이 있어야 한다. 인사·계산·심사 조회처럼 문서와 상관없는 답은 그대로 둔다.

어떻게 강제하나:
1. 근거 없는 답은 버리고 (기록에 남기지 않는다), 검색 툴 하나만 주고 "먼저 검색하라"고 다시 묻는다.
2. 그래도 검색을 안 부르면 질문에서 뽑은 검색어로 하네스가 직접 검색을 부른다.
검색도 일반 툴 호출과 똑같이 권한 검사·감사 로그를 거친다. run 당 한 번만 강제한다.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from contracts.harness import LLMToolCall, Message

_CITATION = re.compile(r"근거\s*[:：]|제\s*\d+\s*(?:조|절|항|장)")
# "근거: 검색 결과 없음" 처럼 근거가 없다는 줄 (모델이 규칙을 어기고 자주 붙인다)
_EMPTY_CITATION = re.compile(r"^\s*근거\s*[:：][^\n]*(?:없음|없습니다|없다|찾지 못)[^\n]*$", re.M)
_RULE_QUESTION = re.compile(
    r"규정|내규|지침|규칙|절차|조항|조문|매뉴얼|기준|누가\s*승인|승인\s*권|어떻게\s*(?:적|써|작성|처리|해야)|해야\s*(?:해|하나|돼|합니까|하나요)"
)
# 검색어를 뽑을 때 떼어 낼 조사·어미 (간단한 근사. 형태소 분석기는 쓰지 않는다)
_SUFFIX = re.compile(r"(?:은|는|이|가|을|를|에|의|와|과|로|으로|에서|도|만|해|해요|해줘|할까|하나요|인가요|야|요|\?)+$")
_STOP = {"누가", "어떻게", "언제", "무엇", "뭐", "왜", "건", "것", "좀", "알려줘", "알려", "주세요", "해야", "하는", "할", "때"}

NUDGE = (
    "방금 답은 사내 문서를 확인하지 않고 쓴 것이라 쓸 수 없다. "
    "먼저 사내 문서 검색 툴로 관련 규정을 찾아라. 검색어는 질문의 핵심 단어 두세 개로 짧게 쓴다. 지금은 툴만 호출한다."
)


def strip_empty_citation(text: str) -> str:
    """"근거: 검색 결과 없음" 같은 빈 근거 줄을 지운다 (화면의 참고 문서 표시가 대신한다)."""
    return _EMPTY_CITATION.sub("", text).rstrip()


def keyword_query(text: str, limit: int = 2) -> str:
    """질문에서 검색어를 뽑는다: 조사·어미를 떼고 의미 있는 앞 단어 두 개."""
    words = []
    for raw in re.split(r"[\s,.!?~]+", text):
        w = raw.strip()
        stem = _SUFFIX.sub("", w)
        w = stem if len(stem) >= 2 else w  # "휴가" 의 '가' 처럼 단어 일부를 조사로 잘못 떼지 않게
        if len(w) >= 2 and w not in _STOP:
            words.append(w)
        if len(words) == limit:
            break
    return " ".join(words) or text.strip()[:30]


@dataclass(frozen=True)
class RequireSearch:
    """하네스에 넘기는 설정. search_tool 이 에이전트 툴에 없으면 아무것도 하지 않는다."""

    search_tool: str = "search_doc"
    query_arg: str = "query"
    grounded_tools: frozenset[str] = field(default_factory=lambda: frozenset({"search_doc", "get_doc_chunk", "parse_document"}))

    def needed(self, question: str, answer: str, used_other_tools: bool = False) -> bool:
        """used_other_tools: 이번 run 에서 문서가 아닌 툴(심사 조회 등)이 성공했는가.
        그런 답에 붙은 근거 줄은 대개 형식적인 것이라 질문이 규정을 묻는 경우에만 강제한다."""
        cited = bool(_CITATION.search(_EMPTY_CITATION.sub("", answer)))
        return (cited and not used_other_tools) or bool(_RULE_QUESTION.search(question))

    def nudge(self) -> Message:
        return Message("user", NUDGE)

    def forced_call(self, reply: Message | None, question: str) -> Message:
        """LLM 이 검색을 불렀으면 그 호출만 남기고, 아니면 하네스가 만든 검색 호출."""
        calls = tuple(c for c in (reply.tool_calls if reply else ()) if c.tool_name == self.search_tool)
        if not calls:
            calls = (LLMToolCall(f"forced_{uuid.uuid4().hex[:12]}", self.search_tool,
                                 {self.query_arg: keyword_query(question)}),)
        return Message("assistant", "", tool_calls=calls)
