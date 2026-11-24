"""최종 답 검사 (결정 사항 1번 답변 검증 + 완료 주장 검사). 모델 없이 코드로 본다.

무엇을 보나
1. 숫자: 조회 규칙의 툴로 답했으면, 답의 숫자가 이번 요청에서 성공한 조회 결과에 있어야 한다.
   실행에 성공한 업무의 식별자·수치 인자도 허용한다. 사용자 질문·시스템 지시·과거 결과는 근거가 아니다.
2. 근거: "근거:" 줄의 조항(제N조)·문서 이름이 이번 요청의 조회 결과에 있어야 한다.
3. 완료 주장: "보냈습니다·저장했습니다·등록했습니다…" 라고 썼는데 이번 요청에서 쓰기(write·send) 툴이
   하나도 성공하지 않았으면 거짓이다 (qwen3 가 툴을 부르지 않고 "보냈다"고만 하는 문제).

걸리면 (runner.py)
- 한 번만 다시 쓰게 한다. 툴도 같이 주므로 모델이 이번에는 실제로 툴을 부를 수 있다.
- 그래도 걸리면: 숫자만 틀린 경우 그 문장을 빼고, 나머지는 고정 문구로 답한다.
- 걸린 답은 관리자만 보는 기록(ANSWER_CHECK_LOG_PATH, 기본 logs/answer_checks.jsonl)에 남긴다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tools.number_check import numbers_in

WRITE_EFFECTS = ("write", "send")

# 목록 번호("1. ", "2) "), 근거 번호([1]), 단계 표시는 숫자 검사에서 뺀다
_LIST_NUMBER = re.compile(r"(?m)^\s*(?:[-*]\s*)?\**\d{1,2}[.)]\s")
_CITE_NUMBER = re.compile(r"\[\d{1,2}\]")
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SENTENCE = re.compile(r"[^.!?\n。]+[.!?。]?\s*|\n")

_ARTICLE = re.compile(r"제\s*\d+\s*조(?:\s*의\s*\d+)?")
_CITATION_LINE = re.compile(r"(?m)^\s*(?:\**)?근거\s*[:：](.*)$")
_DOC_NAME = re.compile(r"[\w·()（）-]+(?:규정|지침|세칙|규칙|매뉴얼|내규|\.hwpx|\.hwp|\.pdf|\.docx)")

# 바깥에 남는 일만 본다 (채팅 안에서 "정리했습니다·작성했습니다" 는 답 자체라 거짓이 아니다)
_CLAIM = re.compile(
    r"(?<!못\s)(?<!안\s)(?:보냈|발송했|발송하였|전송했|저장했|저장하였|등록했|등록하였|접수했)(?:습니다|어요|다|음)"
    r"|(?:문서|정리표|보고서|리포트|파일|PDF|DOCX|알림)[^.\n]{0,15}(?:만들었|생성했|작성했|제공합니다|제공했)(?:습니다|어요|다)?"
)


def _date_numbers(text: str) -> set[str]:
    """2026-11-26 → 11.26, 11.2 같은 월.일 표기도 같은 숫자로 본다."""
    found: set[str] = set()
    for _, month, day in _ISO_DATE.findall(text):
        for token in (f"{int(month)}.{day}", f"{int(month)}.{int(day)}"):
            found |= numbers_in(token)
    return found


def answer_numbers(answer: str) -> set[str]:
    text = _CITE_NUMBER.sub(" ", _LIST_NUMBER.sub(" ", answer))
    return numbers_in(text)


def unknown_answer_numbers(answer: str, sources: list[str]) -> set[str]:
    allowed: set[str] = set()
    for source in sources:
        allowed |= numbers_in(source) | _date_numbers(source)
    return answer_numbers(answer) - allowed


def drop_sentences_with(answer: str, numbers: set[str]) -> str:
    """틀린 숫자가 든 문장만 뺀다."""
    kept = [part for part in _SENTENCE.findall(answer) if not (answer_numbers(part) & numbers)]
    return re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()


def citation_problems(answer: str, sources: list[str]) -> list[str]:
    joined = re.sub(r"\s+", "", "\n".join(sources))
    problems = []
    for line in _CITATION_LINE.findall(answer):
        for article in _ARTICLE.findall(line):
            if re.sub(r"\s+", "", article) not in joined:
                problems.append(f"조회 결과에 없는 조항: {article.strip()}")
        for name in _DOC_NAME.findall(line):
            if re.sub(r"\s+", "", name) not in joined:
                problems.append(f"조회 결과에 없는 문서: {name}")
    return problems


def false_completion(answer: str, wrote: bool) -> bool:
    return not wrote and bool(_CLAIM.search(answer))


@dataclass
class Verdict:
    numbers: set[str] = field(default_factory=set)
    citations: list[str] = field(default_factory=list)
    completion: bool = False

    @property
    def ok(self) -> bool:
        return not (self.numbers or self.citations or self.completion)

    def problems(self) -> list[str]:
        out = []
        if self.completion:
            out.append("이번 요청에서 실제로 실행하지 않은 작업(발송·저장·등록 등)을 완료했다고 썼다")
        if self.numbers:
            out.append("조회 결과에 없는 숫자: " + ", ".join(sorted(self.numbers)))
        out += self.citations
        return out

    def nudge(self) -> str:
        return ("[답 검사] 방금 답은 쓸 수 없다: " + "; ".join(self.problems()) + ". "
                "작업을 해야 하면 지금 툴을 호출하라. 아니면 조회 결과에 있는 사실·숫자·조항만으로 다시 답하고, "
                "실행하지 않은 일은 하지 않았다고 말하라.")


def check_answer(answer: str, *, sources: list[str], grounded: bool, wrote: bool) -> Verdict:
    """grounded: 이번 요청에서 조회 규칙의 툴이 성공했는가 (아니면 숫자·근거는 보지 않는다)."""
    verdict = Verdict(completion=false_completion(answer, wrote))
    if grounded:
        verdict.numbers = unknown_answer_numbers(answer, sources)
        verdict.citations = citation_problems(answer, sources)
    return verdict


async def log_blocked(record: dict) -> None:
    """걸린 답 기록 (관리자만 보는 서버 파일). 고객 정보가 섞일 수 있어 답은 앞부분만 남긴다."""
    path = Path(os.environ.get("ANSWER_CHECK_LOG_PATH", "logs/answer_checks.jsonl"))
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **record}, ensure_ascii=False) + "\n"

    def append() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    try:
        await asyncio.to_thread(append)
    except OSError:
        pass
