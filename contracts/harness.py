"""하네스 계약 (에이전트 루프 ↔ LLM / 툴).

흐름:
  messages → LLMClient.chat(messages, tools) → LLMResponse
    tool_calls 가 있으면: 각 호출마다
      PermissionChecker.check() → (ASK 면 ApprovalHandler) → Tool.run() → ToolResult
      결과를 role="tool" 메시지로 붙이고 다시 chat()
    없으면: content 가 최종 답변
  HARNESS_MAX_TURNS 를 넘으면 중단.

LLMClient 는 OpenAI 호환 API(Ollama /v1) 기준으로 맞췄다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from contracts.permission import ToolCall

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class LLMToolCall:
    """LLM 응답 안의 툴 호출 요청 (아직 principal/session 이 붙기 전)."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[LLMToolCall, ...] = ()  # role="assistant" 일 때만
    tool_call_id: str | None = None  # role="tool" 일 때, 어떤 호출의 결과인지
    def to_dict(self):
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "arguments": call.arguments
                }
                for call in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id
        }


    @classmethod
    def from_dict(cls, data):
        return cls(
            role=data["role"],
            content=data.get("content", ""),
            tool_calls=tuple(
                LLMToolCall(**call)
                for call in data.get("tool_calls", [])
            ),
            tool_call_id=data.get("tool_call_id")
        )

@dataclass(frozen=True)
class ToolSpec:
    """LLM 에게 넘기는 툴 설명. parameters 는 JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


@dataclass(frozen=True)
class LLMResponse:
    message: Message  # role="assistant"
    finish_reason: str | None = None


@runtime_checkable
class LLMClient(Protocol):
    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    ok: bool
    content: str  # LLM 에게 돌려줄 텍스트 (에러면 에러 설명)
    data: Any = None  # 화면 표시 등 LLM 외 용도로 쓸 원본 데이터


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    def run(self, call: ToolCall) -> ToolResult: ...


@runtime_checkable
class ToolRegistry(Protocol):
    def specs(self) -> list[ToolSpec]: ...

    def get(self, name: str) -> Tool | None: ...
