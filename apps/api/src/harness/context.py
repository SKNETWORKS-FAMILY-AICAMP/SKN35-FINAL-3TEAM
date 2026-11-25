"""LLM 에 보내기 전에 메시지를 모델의 컨텍스트 한도 안으로 줄인다.

왜 필요한가 (2026-11-23 qwen3:8b, Ollama 0.40 에서 확인):
- Ollama 는 모델 메모리에 맞춰 컨텍스트를 4096 토큰으로 잡는다. OpenAI 호환 경로(/v1)로는 요청마다 바꿀 수 없다.
- 한도를 넘으면 앞부분만 자르는 게 아니라 메시지를 통째로 버린다. 문서 2만 자(약 1만 5천 토큰)를 읽힌 대화가
  82 토큰(시스템 + 마지막 질문)으로 줄었고, 모델은 문서를 못 본 채 "보존 기간 5년"을 지어냈다 (정답 7년).
  아무 오류도 나지 않아서 사용자도 개발자도 모른다.

그래서 보내기 전에 우리가 먼저 줄인다. 줄이는 순서 (앞 단계로 충분하면 멈춘다):
1. 이전 질문에서 쓴 툴 결과를 앞부분만 남기고 줄인다 (이번 질문의 툴 결과는 그대로).
2. 가장 오래된 대화부터 질문 단위로 뺀다 (시스템 프롬프트와 이번 질문 이후는 남긴다).
3. 그래도 넘치면 이번 질문의 가장 큰 툴 결과를 자르고, 잘렸다고 모델에게 알린다.
토큰 수는 어림한다 (한글 1글자 ≈ 1토큰, 그 외 ≈ 0.4토큰. 실측보다 15~30% 크게 잡아 안전 쪽으로).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from contracts.harness import Message, ToolSpec

_HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
PER_MESSAGE = 6  # 역할 표시 등 메시지마다 붙는 토큰
OLD_TOOL_KEEP = 400  # 지난 질문의 툴 결과는 이만큼만 남긴다 (글자)


def estimate_tokens(text: str) -> int:
    hangul = len(_HANGUL.findall(text))
    return hangul + int((len(text) - hangul) * 0.4) + 1


def message_tokens(m: Message) -> int:
    calls = "".join(c.tool_name + json.dumps(c.arguments, ensure_ascii=False) for c in m.tool_calls)
    return PER_MESSAGE + estimate_tokens(m.content) + estimate_tokens(calls)


def tools_tokens(tools: list[ToolSpec]) -> int:
    return sum(estimate_tokens(t.name + t.description + json.dumps(t.parameters, ensure_ascii=False)) + 20 for t in tools)


@dataclass(frozen=True)
class FitReport:
    before: int  # 줄이기 전 어림 토큰
    after: int
    budget: int
    compacted: int = 0  # 줄인 지난 툴 결과 수
    dropped: int = 0  # 뺀 메시지 수
    truncated: int = 0  # 자른 이번 툴 결과 수

    @property
    def changed(self) -> bool:
        return bool(self.compacted or self.dropped or self.truncated)


def _last_user(messages: list[Message]) -> int:
    return max((i for i, m in enumerate(messages) if m.role == "user"), default=len(messages))


def _shorten(m: Message, keep: int, note: str) -> Message:
    if m.role == "tool":
        try:
            data = json.loads(m.content)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            # 본문에 시스템 안내를 섞거나 JSON 중간을 자르지 않는다.
            compact = {key: data[key] for key in ("file_id", "filename", "title", "location") if key in data}
            compact.update(text="", truncated=True,
                           read_status={"document_complete": False, "context_truncated": True})
            content = data["text"]
            available = max(0, keep - len(json.dumps(compact, ensure_ascii=False)))
            compact["text"] = content[:available]
            packed = json.dumps(compact, ensure_ascii=False)
            while len(packed) > keep and compact["text"]:
                compact["text"] = compact["text"][:-(len(packed) - keep)]
                packed = json.dumps(compact, ensure_ascii=False)
            return replace(m, content=packed)
    return replace(m, content=m.content[:keep] + f"\n…({note}: 원래 {len(m.content):,}자)")


def fit_messages(messages: list[Message], tools: list[ToolSpec], limit: int, reserve: int) -> tuple[list[Message], FitReport]:
    """limit = 모델 컨텍스트 토큰, reserve = 답(생각 포함)에 남겨 둘 토큰."""
    budget = max(limit - reserve - tools_tokens(tools), 256)
    msgs = list(messages)
    total = lambda: sum(message_tokens(m) for m in msgs)  # noqa: E731
    before = total()
    compacted = dropped = truncated = 0
    if before <= budget:
        return msgs, FitReport(before, before, budget)

    # 1. 지난 질문의 툴 결과 줄이기
    cut = _last_user(msgs)
    for i in range(cut):
        m = msgs[i]
        if m.role == "tool" and len(m.content) > OLD_TOOL_KEEP:
            msgs[i] = _shorten(m, OLD_TOOL_KEEP, "이전 툴 결과 생략")
            compacted += 1
    # 2. 오래된 질문 단위로 빼기 (툴 호출과 그 결과가 짝을 잃지 않게 user 메시지 경계로만 자른다)
    while total() > budget:
        cut = _last_user(msgs)
        first = next((i for i, m in enumerate(msgs) if m.role != "system"), cut)
        if first >= cut:
            break
        nxt = next((i for i in range(first + 1, cut + 1) if i == cut or msgs[i].role == "user"), cut)
        dropped += nxt - first
        del msgs[first:nxt]
    # 3. 이번 질문의 큰 툴 결과 자르기 (큰 것부터)
    done: set[int] = set()  # 한 메시지는 한 번만 자른다 (더 못 줄이면 멈춘다)
    while total() > budget:
        cut = _last_user(msgs)
        tools_now = [i for i in range(cut, len(msgs))
                     if i not in done and msgs[i].role == "tool" and len(msgs[i].content) > OLD_TOOL_KEEP]
        if not tools_now:
            break
        i = max(tools_now, key=lambda k: len(msgs[k].content))
        m = msgs[i]
        room = budget - (total() - message_tokens(m)) - PER_MESSAGE - 80  # 이 메시지에 쓸 수 있는 토큰 (안내 문구 몫 80)
        lo, hi = OLD_TOOL_KEEP, len(m.content)  # 들어가는 가장 긴 앞부분을 찾는다
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if estimate_tokens(m.content[:mid]) <= room else (lo, mid - 1)
        msgs[i] = _shorten(m, lo, "길이 한도 때문에 앞부분만 전달함. 나머지가 필요하면 범위를 나눠 다시 읽어라")
        truncated += 1
        done.add(i)
    return msgs, FitReport(before, total(), budget, compacted, dropped, truncated)
