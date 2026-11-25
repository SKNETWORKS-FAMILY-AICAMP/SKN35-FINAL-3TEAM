"""시나리오 JSON 을 실제 객체로 바꿔주는 가짜 구현들. 구현이 아니라 테스트 도구."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from contracts.harness import (
    LLMResponse,
    LLMToolCall,
    Message,
    ToolResult,
    ToolSpec,
)
from contracts.permission import (
    ApprovalOutcome,
    AuditRecord,
    Decision,
    PermissionResult,
    ToolCall,
)


class ScriptedLLM:
    """script 순서대로 응답한다. 받은 messages/tools 는 calls 에 기록."""

    def __init__(self, script: list[dict[str, Any]]):
        self._script = list(script)
        self.calls: list[tuple[list[Message], list[ToolSpec]]] = []

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append((list(messages), list(tools)))
        idx = len(self.calls) - 1
        if idx >= len(self._script):
            raise AssertionError(f"LLM 이 스크립트({len(self._script)}개)보다 많이 호출됨")
        step = self._script[idx]
        if "raises" in step:
            raise RuntimeError(step["raises"])
        tool_calls = tuple(
            LLMToolCall(call_id=tc["id"], tool_name=tc["name"], arguments=tc["arguments"])
            for tc in step.get("tool_calls", [])
        )
        msg = Message(role="assistant", content=step.get("content", ""), tool_calls=tool_calls)
        return LLMResponse(message=msg, finish_reason="tool_calls" if tool_calls else "stop")


class FakeTool:
    def __init__(
        self, name: str, output: str = "", raises: str | None = None,
        log: list[str] | None = None, display_name: str = "",
    ):
        self.spec = ToolSpec(name=name, description=f"fake {name}", display_name=display_name)
        self._output = output
        self._raises = raises
        self._log = log if log is not None else []

    async def run(self, call: ToolCall) -> ToolResult:
        self._log.append(self.spec.name)
        if self._raises:
            raise RuntimeError(self._raises)
        return ToolResult(call_id=call.call_id, ok=True, content=self._output)


class FakeRegistry:
    def __init__(self, tools: list[FakeTool]):
        self._tools = {t.spec.name: t for t in tools}

    async def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    async def get(self, name: str) -> FakeTool | None:
        return self._tools.get(name)


class ScriptedChecker:
    def __init__(self, table: dict[str, str]):
        self._table = table
        self.calls: list[ToolCall] = []

    async def check(self, call: ToolCall) -> PermissionResult:
        self.calls.append(call)
        decision = Decision(self._table.get(call.tool_name, "allow"))
        return PermissionResult(decision=decision, reason=f"scripted {decision.value}")


class FixedApprover:
    def __init__(self, answer: ApprovalOutcome | bool):
        if isinstance(answer, bool):
            answer = "approved" if answer else "rejected"
        self._answer: ApprovalOutcome = answer
        self.requests: list[str] = []

    async def request_approval(self, call: ToolCall, result: PermissionResult) -> ApprovalOutcome:
        self.requests.append(call.call_id)
        return self._answer


@dataclass
class MemoryAudit:
    records: list[AuditRecord] = field(default_factory=list)

    async def log(self, record: AuditRecord) -> None:
        self.records.append(record)


@dataclass
class ScenarioWorld:
    """시나리오 하나를 돌리는 데 필요한 가짜 객체 묶음."""

    llm: ScriptedLLM
    tools: FakeRegistry
    checker: ScriptedChecker
    approver: FixedApprover
    audit: MemoryAudit
    executed: list[str]

    @classmethod
    def from_scenario(cls, sc: dict[str, Any]) -> ScenarioWorld:
        executed: list[str] = []
        tools = [
            FakeTool(t["name"], output=t.get("output", ""), raises=t.get("raises"), log=executed,
                     display_name=t.get("display_name", ""))
            for t in sc.get("tools", [])
        ]
        return cls(
            llm=ScriptedLLM(sc["llm_script"]),
            tools=FakeRegistry(tools),
            checker=ScriptedChecker(sc.get("permissions", {})),
            approver=FixedApprover(sc.get("approval", "rejected")),
            audit=MemoryAudit(),
            executed=executed,
        )
