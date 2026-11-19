import re
from uuid import uuid4

from contracts.harness import LLMResponse, LLMToolCall, Message, ToolSpec


_NUMBER = r"[+-]?\d{1,30}(?:\.\d{1,30})?"
_SYMBOLIC = re.compile(
    rf"\s*({_NUMBER})\s*([+*×-])\s*({_NUMBER})\s*(?:계산해\s*줘)?\s*[?]?\s*"
)
_PARENTHESIZED = re.compile(
    rf"\s*\(\s*({_NUMBER})\s*([+*×-])\s*({_NUMBER})\s*\)"
    rf"\s*([+*×-])\s*({_NUMBER})\s*(?:계산해\s*줘)?\s*[?]?\s*"
)
_KOREAN = re.compile(
    rf"\s*({_NUMBER})(?:과|와|에서)\s*({_NUMBER})(?:을|를)\s*(더해|빼|곱해)\s*줘\s*[?]?\s*"
)
_TOOL_NAMES = {
    "+": "add", "-": "subtract", "*": "multiply", "×": "multiply",
    "더해": "add", "빼": "subtract", "곱해": "multiply",
}

class LocalMathClient:
    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        latest = messages[-1]
        if latest.role == "tool":
            try:
                float(latest.content)
            except ValueError:
                return LLMResponse(Message(role="assistant", content=latest.content))
            if len(messages) >= 3 and messages[-3].role == "user":
                expression = _PARENTHESIZED.fullmatch(messages[-3].content)
                if expression:
                    _, _, _, operation, right = expression.groups()
                    return self._tool_call(latest.content, operation, right)
            return LLMResponse(
                Message(role="assistant", content=f"계산 결과는 {latest.content}입니다.")
            )

        symbolic = _SYMBOLIC.fullmatch(latest.content)
        korean = _KOREAN.fullmatch(latest.content)
        expression = _PARENTHESIZED.fullmatch(latest.content)
        if expression:
            left, operation, right, _, _ = expression.groups()
        elif symbolic:
            left, operation, right = symbolic.groups()
        elif korean:
            left, right, operation = korean.groups()
        else:
            return LLMResponse(Message(
                role="assistant",
                content="숫자 두 개의 덧셈, 뺄셈, 곱셈을 입력해 주세요. 예를 들어 7 * 8 또는 7과 8을 곱해줘.",
            ))

        return self._tool_call(left, operation, right)

    def _tool_call(self, left: str, operation: str, right: str) -> LLMResponse:
        call = LLMToolCall(
            call_id=str(uuid4()),
            tool_name=_TOOL_NAMES[operation],
            arguments={
                "a": float(left) if "." in left or "e" in left else int(left),
                "b": float(right) if "." in right else int(right),
            },
        )
        return LLMResponse(Message(role="assistant", tool_calls=(call,)))
