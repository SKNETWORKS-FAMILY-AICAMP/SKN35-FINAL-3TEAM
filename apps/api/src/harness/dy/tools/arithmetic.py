from contracts.harness import ToolSpec, ToolResult
from contracts.permission import ToolCall


# 1. 실제 계산 함수
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

# 2. Tool 설명
PARAMETERS = {
    "type": "object",
    "properties": {
        "a": {"type": "number"},
        "b": {"type": "number"}
    },
    "required": ["a", "b"]
}


# 3. Harness 연결용 클래스
class MathTool:
    def __init__(self, name, description, func):
        self.spec = ToolSpec(
            name=name,
            description=description,
            parameters=PARAMETERS
        )
        self.func = func

    def run(self, call: ToolCall) -> ToolResult:
        try:
            a = call.arguments["a"]
            b = call.arguments["b"]

            if type(a) not in (int, float) or type(b) not in (int, float):
                raise ValueError("숫자만 입력하세요")

            result = self.func(a, b)

            return ToolResult(
                call.call_id,
                True,
                str(result),
                data=result
            )

        except (KeyError, ValueError) as exc:
            return ToolResult(
                call.call_id,
                False,
                f"계산 오류: {exc}"
            )


# 4. Tool 생성
AddTool = MathTool("add", "두 숫자를 더합니다.", add)
SubtractTool = MathTool("subtract", "두 숫자를 뺍니다.", subtract)
MultiplyTool = MathTool("multiply", "두 숫자를 곱합니다.", multiply)