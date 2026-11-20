"""ToolRegistry 구현: 등록된 툴을 이름으로 찾는다 (contracts.harness.ToolRegistry)."""

from __future__ import annotations

from collections.abc import Iterable

from contracts.harness import Tool, ToolSpec


class SimpleToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"툴 이름 '{name}' 이 이미 등록됨")
        self._tools[name] = tool

    def only(self, names: Iterable[str]) -> SimpleToolRegistry:
        """에이전트별 툴 목록(agent_tool)만 남긴 새 레지스트리. 모르는 이름은 에러."""
        names = list(names)
        unknown = [n for n in names if n not in self._tools]
        if unknown:
            raise ValueError(f"등록되지 않은 툴: {', '.join(unknown)}")
        return SimpleToolRegistry(self._tools[n] for n in names)

    async def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    async def get(self, name: str) -> Tool | None:
        return self._tools.get(name)
