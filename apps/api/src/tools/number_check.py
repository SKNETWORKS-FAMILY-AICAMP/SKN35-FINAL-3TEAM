"""모델이 쓴 문장의 숫자가 조회 결과에 있는 숫자인지 검사한다.

UC-03 5a: 안내 문장에 목록에 없는 숫자가 있으면 문장을 빼고 목록만 보낸다.
같은 방식이 답변 검증(결정사항 1번, "이번 대화의 조회 툴 결과 전체" 기준)에도 쓰일 수 있게 따로 둔다.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _normalize(token: str) -> str:
    value = token.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


def numbers_in(text: str) -> set[str]:
    return {_normalize(token) for token in _NUMBER.findall(text)}


def unknown_numbers(sentence: str, *sources: str) -> set[str]:
    """sentence 에 있지만 sources 어디에도 없는 숫자."""
    allowed: set[str] = set()
    for source in sources:
        allowed |= numbers_in(source)
    return numbers_in(sentence) - allowed
