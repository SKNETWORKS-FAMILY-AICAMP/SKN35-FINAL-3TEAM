class SimpleToolRegistry:
    def __init__(self, tools):
        self.tools = {
            tool.spec.name: tool
            for tool in tools
        }

    # LLM에게 전달할 Tool 설명 목록 반환
    def specs(self):
        return [
            tool.spec
            for tool in self.tools.values()
        ]

    # LLM이 호출한 Tool의 실제 객체 반환
    def get(self, name):
        return self.tools.get(name)
