"""데모용 사칙연산 툴 (add, subtract, multiply). 프론트엔드 데모와 같은 ID 를 쓴다."""

from __future__ import annotations

import operator
from collections.abc import Callable
from numbers import Real

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall

_PARAMS = {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"],
}


class ArithmeticTool:
    def __init__(
        self, name: str, display_name: str, description: str, op: Callable[[float, float], float]
    ) -> None:
        self.spec = ToolSpec(
            name=name, description=description, parameters=_PARAMS, display_name=display_name,
            effect="read", domain="계산",
        )
        self._op = op

    async def run(self, call: ToolCall) -> ToolResult:
        a, b = call.arguments.get("a"), call.arguments.get("b")
        if not all(isinstance(x, Real) and not isinstance(x, bool) for x in (a, b)):
            return ToolResult(call.call_id, False, "a, b 는 숫자여야 합니다")
        value = self._op(a, b)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ToolResult(call.call_id, True, str(value), data=value)


def arithmetic_tools() -> list[ArithmeticTool]:
    return [
        ArithmeticTool("add", "덧셈", "두 수를 더한다", operator.add),
        ArithmeticTool("subtract", "뺄셈", "a 에서 b 를 뺀다", operator.sub),
        ArithmeticTool("multiply", "곱셈", "두 수를 곱한다", operator.mul),
    ]
