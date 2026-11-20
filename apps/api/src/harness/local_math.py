"""LLM 없이 사칙연산 데모를 돌리는 가짜 LLMClient (개발·시연용, dy 구현에서 옮김).

Ollama 가 없을 때도 하네스·권한·API·프론트 흐름을 끝까지 확인할 수 있다.
"7 * 8", "7과 8을 곱해줘", "(2 + 3) * 4" 같은 입력을 읽어 add/subtract/multiply 툴 호출을 만들고,
툴 결과가 오면 "계산 결과는 N입니다." 로 답한다. 툴이 거부·실패하면 그 내용을 그대로 돌려준다.

쓰는 법: .env 에 LLM_PROVIDER=local_math (services/container.py 참고)
"""

from __future__ import annotations

import re
from uuid import uuid4

from contracts.harness import LLMResponse, LLMToolCall, Message, ToolSpec

_NUMBER = r"[+-]?\d{1,30}(?:\.\d{1,30})?"
_SUFFIX = r"(?:\s*(?:계산해\s*줘|은|는))?\s*[?]?\s*"
_SYMBOLIC = re.compile(rf"\s*({_NUMBER})\s*([+*×-])\s*({_NUMBER}){_SUFFIX}")
_PARENTHESIZED = re.compile(
    rf"\s*\(\s*({_NUMBER})\s*([+*×-])\s*({_NUMBER})\s*\)\s*([+*×-])\s*({_NUMBER}){_SUFFIX}"
)
_KOREAN = re.compile(rf"\s*({_NUMBER})(?:과|와|에서)\s*({_NUMBER})(?:을|를)\s*(더해|빼|곱해)\s*줘\s*[?]?\s*")
_TOOL_NAMES = {
    "+": "add", "-": "subtract", "*": "multiply", "×": "multiply",
    "더해": "add", "빼": "subtract", "곱해": "multiply",
}
HELP = "숫자 두 개의 덧셈, 뺄셈, 곱셈을 입력해 주세요. 예: 7 * 8 또는 7과 8을 곱해줘."


def _num(text: str) -> int | float:
    return float(text) if "." in text else int(text)


class LocalMathClient:
    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        latest = messages[-1]
        if not tools:  # 거부 후 마무리 호출 (tools=[]): 지시문을 읽을 수 없으니 비워서 고정 문구로 넘긴다
            return LLMResponse(Message(role="assistant", content=""))

        if latest.role == "tool":
            try:
                float(latest.content)
            except ValueError:  # 툴 에러·거부 메시지
                return LLMResponse(Message(role="assistant", content=latest.content))
            user = next((m for m in reversed(messages) if m.role == "user"), None)
            expr = _PARENTHESIZED.fullmatch(user.content) if user else None
            tool_turns = sum(1 for m in messages[messages.index(user):] if m.role == "tool") if user else 0
            if expr and tool_turns == 1:  # (a op b) op c 의 두 번째 단계
                _, _, _, op, right = expr.groups()
                return self._tool_call(latest.content, op, right, tools)
            return LLMResponse(Message(role="assistant", content=f"계산 결과는 {latest.content}입니다."))

        text = latest.content
        if m := _PARENTHESIZED.fullmatch(text):
            left, op, right, _, _ = m.groups()
        elif m := _SYMBOLIC.fullmatch(text):
            left, op, right = m.groups()
        elif m := _KOREAN.fullmatch(text):
            left, right, op = m.groups()
        else:
            return LLMResponse(Message(role="assistant", content=HELP))
        return self._tool_call(left, op, right, tools)

    @staticmethod
    def _tool_call(left: str, op: str, right: str, tools: list[ToolSpec]) -> LLMResponse:
        name = _TOOL_NAMES[op]
        if name not in {t.name for t in tools}:  # 이 에이전트에 없는 툴은 부르지 않는다 (실제 LLM 처럼)
            return LLMResponse(Message(role="assistant", content=f"이 에이전트에는 '{name}' 도구가 없습니다."))
        call = LLMToolCall(call_id=str(uuid4()), tool_name=name, arguments={"a": _num(left), "b": _num(right)})
        return LLMResponse(Message(role="assistant", tool_calls=(call,)))
