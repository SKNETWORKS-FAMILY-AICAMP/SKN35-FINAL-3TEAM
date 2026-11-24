"""조회 규칙 — "이런 질문은 이 조회 툴의 실제 결과 없이 답하면 안 된다" 를 한 목록(DEFAULT_RULES)으로 둔다.

작은 모델은 "찾아보고 답하라"는 프롬프트를 자주 무시하고, 조회 없이 그럴듯한 규정·건수·수치를 지어낸다
(qwen3 8b·14b 모두 확인). 그래서 하네스가 최종 답을 받기 전에 규칙마다 한 번 검사한다.

규칙 하나 = LookupRule: 어떤 툴(tool)인지, 어떤 질문·답이면 필요한지(needed), 질문에서 인자를 어떻게 뽑는지(arguments),
어떤 툴이 성공하면 조회한 것으로 보는지(grounded_tools).
  - 사내 문서 RequireSearch  search_doc      규정을 묻거나 답에 "근거:"·조항 표시가 있을 때
  - 고객 안내 NoticeLookup   prepare_maturity_notices  만기 고객 안내 메일을 준비·발송해 달라고 할 때 (UC-05-1)
  - 만기 대출 LoanLookup     loans_maturing  만기 대출 건수·목록을 물을 때 (UC-03). 없으면 안내문 준비 툴로
  - 금리·환율 MarketLookup   market_snapshot 금리·환율 수치를 물을 때 (UC-02)
에이전트에 그 툴이 있을 때만 적용한다.

어떻게 강제하나 (하네스 runner.py, 규칙마다 run 당 한 번):
1. 조회 없이 쓴 답은 버리고 (기록에 남기지 않는다), 그 툴 하나만 주고 "먼저 조회하라"고 다시 묻는다.
2. 그래도 부르지 않으면 질문에서 뽑은 인자로 하네스가 직접 부른다.
조회도 일반 툴 호출과 똑같이 권한 검사·감사 로그를 거친다.
조회한 뒤의 답은 harness/answer_check.py 가 숫자·근거·완료 주장을 한 번 더 본다.

심사 건 조회(harness/review_grounding.py)는 등록(쓰기)까지 이어지는 흐름이라 따로 둔다 (UC-04, 이번 범위 밖).
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
class LookupRule:
    """조회 규칙 하나. 하위 클래스가 needed·arguments·nudge_text 를 정한다."""

    tool: str
    grounded_tools: frozenset[str]
    unavailable: str = "요청한 내용을 실제로 조회하지 못해 답할 수 없습니다. 잠시 뒤 다시 요청해 주세요."
    alternatives: tuple[str, ...] = ()  # 에이전트에 tool 이 없을 때 대신 부를 조회 툴 (같은 인자)

    def available(self, names) -> str | None:
        """에이전트가 가진 툴 중 이 규칙이 부를 툴 (없으면 None → 규칙을 쓰지 않음)."""
        return next((name for name in (self.tool, *self.alternatives) if name in names), None)

    def needed(self, question: str, answer: str, used_other_tools: bool = False) -> bool:
        raise NotImplementedError

    def arguments(self, question: str) -> dict | None:
        """하네스가 직접 부를 때의 인자. None 이면 직접 부르지 않는다."""
        return {}

    def nudge_text(self, tool: str) -> str:
        return f"방금 답은 실제 조회 없이 쓴 것이라 쓸 수 없다. 지금은 {tool} 툴만 호출하라."

    def nudge(self, tool: str | None = None) -> Message:
        return Message("user", self.nudge_text(tool or self.tool))

    def forced_call(self, reply: Message | None, question: str, tool: str | None = None) -> Message | None:
        """LLM 이 이 툴을 불렀으면 그 호출만 남기고, 아니면 하네스가 만든 호출. 만들 수 없으면 None."""
        tool = tool or self.tool
        calls = tuple(c for c in (reply.tool_calls if reply else ()) if c.tool_name == tool)
        if not calls:
            args = self.arguments(question)
            if args is None:
                return None
            calls = (LLMToolCall(f"forced_{uuid.uuid4().hex[:12]}", tool, args),)
        return Message("assistant", "", tool_calls=calls)


@dataclass(frozen=True)
class RequireSearch(LookupRule):
    """사내 문서 검색. search_tool 이 에이전트 툴에 없으면 아무것도 하지 않는다."""

    tool: str = "search_doc"
    grounded_tools: frozenset[str] = field(default_factory=lambda: frozenset({"search_doc", "get_doc_chunk", "parse_doc"}))
    unavailable: str = "등록된 규정에서 근거를 찾지 못했습니다."
    query_arg: str = "query"

    @property
    def search_tool(self) -> str:
        return self.tool

    def needed(self, question: str, answer: str, used_other_tools: bool = False) -> bool:
        """used_other_tools: 이번 run 에서 문서가 아닌 툴(심사 조회 등)이 성공했는가.
        그런 답에 붙은 근거 줄은 대개 형식적인 것이라 질문이 규정을 묻는 경우에만 강제한다."""
        cited = bool(_CITATION.search(_EMPTY_CITATION.sub("", answer)))
        return (cited and not used_other_tools) or bool(_RULE_QUESTION.search(question))

    def arguments(self, question: str) -> dict:
        return {self.query_arg: keyword_query(question)}

    def nudge_text(self, tool: str) -> str:
        return NUDGE

    def retry_arguments(self, question: str, tried: set[str]) -> dict | None:
        """검색 결과가 0건일 때 한 번 더 찾을 검색어 (결정 사항 1번). 이미 쓴 검색어와 다른 것만."""
        for query in (keyword_query(question, 1), keyword_query(question, 3)):
            if query and query not in tried:
                return {self.query_arg: query}
        return None


_LOAN_QUESTION = re.compile(r"만기")
_LOAN_TARGET = re.compile(r"대출|고객|여신|건")
_DAYS = re.compile(r"(?<!\d)(\d{1,3})\s*일")


@dataclass(frozen=True)
class LoanLookup(LookupRule):
    """내 담당 만기 대출 (UC-03). 건수·고객·만기일은 조회 결과로만 답한다."""

    tool: str = "loans_maturing"
    grounded_tools: frozenset[str] = field(default_factory=lambda: frozenset(
        {"loans_maturing", "send_maturity_alert", "prepare_maturity_notices", "send_maturity_notice", "fill_document"}))
    unavailable: str = "담당 대출을 조회하지 못해 만기 대출을 안내할 수 없습니다. 잠시 뒤 다시 요청해 주세요."
    alternatives: tuple[str, ...] = ("prepare_maturity_notices",)  # UC-05 에이전트는 안내문 준비로 조회한다

    def needed(self, question: str, answer: str, used_other_tools: bool = False) -> bool:
        return bool(_LOAN_QUESTION.search(question) and _LOAN_TARGET.search(question)
                    and not _RULE_QUESTION.search(question))

    def arguments(self, question: str) -> dict:
        found = _DAYS.search(question)
        days = int(found.group(1)) if found else 30
        return {"days": min(max(days, 1), 90)}

    def nudge_text(self, tool: str) -> str:
        return ("방금 답은 담당 대출을 조회하지 않고 쓴 것이라 쓸 수 없다. 건수·고객·만기일을 지어내지 말고 "
                f"지금은 {tool} 툴만 호출하라. 기간(days)은 질문에 있으면 그 값, 없으면 30.")


_NOTICE_QUESTION = re.compile(r"(?:안내|메일)")
_NOTICE_ACTION = re.compile(r"준비|만들|보내|발송|작성")


@dataclass(frozen=True)
class NoticeLookup(LookupRule):
    """고객 만기 안내 준비 (UC-05-1). 대출 목록만 보고 "준비하겠습니다"라고 답하지 않게, 안내문 준비 툴을 부르게 한다.
    (실측: 커스텀 에이전트가 만기 대출 조회만 하고 안내문은 준비하지 않은 채 끝냄)"""

    tool: str = "prepare_maturity_notices"
    grounded_tools: frozenset[str] = field(default_factory=lambda: frozenset(
        {"prepare_maturity_notices", "send_maturity_notice"}))
    unavailable: str = "안내문을 준비하지 못했습니다. 잠시 뒤 다시 요청해 주세요."

    def needed(self, question: str, answer: str, used_other_tools: bool = False) -> bool:
        return bool(_LOAN_QUESTION.search(question) and _NOTICE_QUESTION.search(question)
                    and _NOTICE_ACTION.search(question) and not _RULE_QUESTION.search(question))

    def arguments(self, question: str) -> dict:
        found = _DAYS.search(question)
        return {"days": min(max(int(found.group(1)), 1), 90) if found else 7}

    def nudge_text(self, tool: str) -> str:
        return (f"방금 답은 안내문을 실제로 준비하지 않고 쓴 것이라 쓸 수 없다. 지금은 {tool} 툴만 호출하라. "
                "기간(days)은 질문에 있으면 그 값, 없으면 7.")


_MARKET_QUESTION = re.compile(r"금리|환율")
_CONCEPT = re.compile(r"이란|란\s*뭐|무슨\s*뜻|개념|의미|원리")
_ISO_DAY = re.compile(r"(\d{4})[-년.]\s*(\d{1,2})[-월.]\s*(\d{1,2})")


@dataclass(frozen=True)
class MarketLookup(LookupRule):
    """금리·환율 수치 (UC-02). 수치는 시장 조회 결과로만 답한다."""

    tool: str = "market_snapshot"
    grounded_tools: frozenset[str] = field(default_factory=lambda: frozenset(
        {"market_snapshot", "save_morning_report", "get_morning_report"}))
    unavailable: str = "금리·환율을 조회하지 못해 수치를 안내할 수 없습니다. 잠시 뒤 다시 요청해 주세요."

    def needed(self, question: str, answer: str, used_other_tools: bool = False) -> bool:
        return bool(_MARKET_QUESTION.search(question) and not _CONCEPT.search(question))

    def arguments(self, question: str) -> dict:
        found = _ISO_DAY.search(question)
        return {"base_date": f"{found[1]}-{int(found[2]):02d}-{int(found[3]):02d}"} if found else {}

    def nudge_text(self, tool: str) -> str:
        return f"방금 답은 시장 수치를 조회하지 않고 쓴 것이라 쓸 수 없다. 수치를 지어내지 말고 지금은 {tool} 툴만 호출하라."


DEFAULT_RULES: tuple[LookupRule, ...] = (RequireSearch(), NoticeLookup(), LoanLookup(), MarketLookup())
