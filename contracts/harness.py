"""하네스 계약 (에이전트 루프 ↔ LLM / 툴).

흐름:
  messages → await LLMClient.chat(messages, tools) → LLMResponse
    tool_calls 가 있으면: 각 호출마다
      PermissionChecker.check() → (ASK 면 ApprovalHandler) → Tool.run() → ToolResult
      결과를 role="tool" 메시지로 붙이고 다시 chat()
    없으면: content 가 최종 답변
  HARNESS_MAX_TURNS 를 넘으면 중단.

LLMClient 는 OpenAI 호환 API(Ollama /v1) 기준으로 맞췄다.

모든 I/O 경계(LLM·툴·권한·승인·감사)는 async 다. 구현 안에서 블로킹 호출
(requests, time.sleep, 동기 DB 드라이버, 무거운 연산 등)을 하면 이벤트 루프가 멈추므로
반드시 async 라이브러리를 쓰거나 asyncio.to_thread() 로 감싼다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from contracts.permission import (
    ApprovalHandler,
    AuditLogger,
    PermissionChecker,
    Principal,
    ToolCall,
)

Role = Literal["system", "user", "assistant", "tool"]
ToolEffect = Literal["read", "write", "send"]  # ToolSpec.effect 참고


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

    # 저장용 직렬화 (세션 기록 등). OpenAI API 형식과는 다르다 (그 변환은 LLMClient 구현 몫).
    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {"call_id": c.call_id, "tool_name": c.tool_name, "arguments": c.arguments}
                for c in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=data["role"],
            content=data.get("content", ""),
            tool_calls=tuple(LLMToolCall(**c) for c in data.get("tool_calls", [])),
            tool_call_id=data.get("tool_call_id"),
        )


@dataclass(frozen=True)
class ToolSpec:
    """LLM 에게 넘기는 툴 설명. parameters 는 JSON Schema.

    display_name 은 사용자 화면·안내 문구에 쓰는 이름이다 (예: "메일 발송").
    LLM 에게는 넘기지 않는다. 비어 있으면 name 을 쓴다 (label).

    effect 는 툴이 하는 일의 종류다. 툴을 만드는 사람이 정한다.
      read  조회만 한다 (문서 검색, 날짜 확인)
      write 사내 시스템의 데이터를 만들거나 바꾼다 (심사 요청 등록)
      send  사람이나 바깥으로 무언가를 보낸다 (메일·알림 발송)
    정하지 않으면 write 로 본다 (모르는 툴은 위험한 쪽으로).
    에이전트 초안을 만들 때 LLM 은 read 툴만 바로 넣고, write·send 툴은 추천만 한다 (사용자가 직접 체크).
    실행 권한은 이것과 별개로 ABAC 정책이 판정한다.

    domain 은 화면에서 툴을 묶어 보여 주는 업무 분류다 (예: "문서", "심사"). 비어 있어도 된다.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    display_name: str = ""
    effect: ToolEffect = "write"
    domain: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True)
class LLMResponse:
    message: Message  # role="assistant"
    finish_reason: str | None = None


@runtime_checkable
class LLMClient(Protocol):
    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...


@runtime_checkable
class RequiredToolLLMClient(LLMClient, Protocol):
    """명시적으로 요청된 실행 단계에서 특정 도구 호출을 요구할 수 있는 선택 기능."""

    async def chat_required_tool(self, messages: list[Message], tool: ToolSpec) -> LLMResponse: ...


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    ok: bool
    content: str  # LLM 에게 돌려줄 텍스트 (에러면 에러 설명)
    data: Any = None  # 화면 표시 등 LLM 외 용도로 쓸 원본 데이터


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def run(self, call: ToolCall) -> ToolResult: ...


@runtime_checkable
class ToolRegistry(Protocol):
    async def specs(self) -> list[ToolSpec]: ...

    async def get(self, name: str) -> Tool | None: ...


# ── 에이전트 루프 ────────────────────────────────────────────
#
# 동작 규칙 (공통 테스트 tests/test_harness_contract.py 가 이걸 검사한다):
# 0. 모든 프로토콜 메서드는 await 해서 호출한다.
# 1. LLMClient.chat() 은 한 run 에서 최대 max_turns 번까지만 호출한다.
#    max_turns 번째 응답에도 tool_calls 가 있으면 stop_reason="max_turns" 로 끝낸다.
# 2. LLM 이 요청한 tool_call 하나하나에 반드시 role="tool" 메시지(tool_call_id 일치)를
#    붙인 뒤 다음 chat() 을 호출한다. 실패·거부도 예외 없이 메시지로 돌려준다.
# 3. 툴 처리 순서: ToolRegistry.get() → 없으면 에러 메시지 (권한 검사·감사 로그 없음)
#    → PermissionChecker.check() → ASK 면 ApprovalHandler → 허용되면 Tool.run()
#    → 권한을 검사한 호출은 모두 AuditLogger.log() 로 남긴다.
# 4. DENY, 또는 ASK 인데 승인되지 않았으면(rejected·timeout) PermissionResult.reason 을
#    툴 메시지 content 에 포함한다. AuditRecord.approval 에는 ApprovalHandler 가 돌려준 값을 그대로 넣는다.
# 5. Tool.run() 이 예외를 던져도 run 이 죽으면 안 된다. 에러를 툴 메시지로 돌려준다.
#    (이때 AuditRecord.executed 는 True: "run() 을 호출했는가" 의 의미, tool_ok 는 False)
#    AuditRecord.tool_ok: 실행했으면 ToolResult.ok (예외·시간 초과는 False), 실행 안 했으면 None.
# 6. 한 응답 안의 tool_call 들은 요청 순서대로 하나씩 처리한다 (병렬 X).
#    툴 메시지·감사 로그 순서가 tool_calls 순서와 같아야 한다.
# 7. run() 의 agent_id / run_id 는 그 run 에서 만드는 모든 ToolCall 에 그대로 넣는다.
# 8. 권한 거부(DENY, 또는 ASK 인데 rejected·timeout)가 나면 그 run 은 즉시 멈춘다 (우회 시도 차단).
#    a. 거부된 호출에도 사유가 담긴 툴 메시지를 붙인다 (규칙 2, 4).
#    b. 같은 응답의 남은 tool_call 은 실행하지 않는다. 권한 검사·감사 로그 없이
#       "실행하지 않음" 툴 메시지만 붙인다 (규칙 2 유지).
#    c. 마무리 답변 (final_message, role="assistant"):
#       - LLM 을 추가 호출하지 않고 서버가 거부 종류에 맞는 고정 문구를 만든다.
#         문구에는 PermissionResult.reason 을 포함한다 (DENY 일 때).
#         툴은 ToolSpec.label(표시 이름)로 부른다.
#       - 이번 run 에서 성공(ok)한 툴이 있으면 실제 완료 결과를 별도 항목으로 함께 안내한다.
#         실행하지 않은 작업의 성공 여부를 LLM 에게 다시 생성시키지 않는다.
#    d. stop_reason="denied", RunResult.denial 에 거부 정보를 담는다.
#       Denial.kind 는 deny(정책) / rejected(사용자 거절) / timeout(승인 시간 초과) 를 구분한다.
#    (툴 실행 실패·없는 툴은 거부가 아니다. 그때는 에러를 돌려주고 계속 진행한다.)


StopReason = Literal["completed", "max_turns", "denied"]
DenialKind = Literal["deny", "rejected", "timeout"]  # deny=정책, rejected·timeout=ApprovalOutcome


@dataclass(frozen=True)
class Denial:
    call_id: str
    tool_name: str
    kind: DenialKind
    reason: str  # PermissionResult.reason
    display_name: str = ""  # ToolSpec.display_name (화면용)

    @property
    def label(self) -> str:
        return self.display_name or self.tool_name


@dataclass(frozen=True)
class RunResult:
    final_message: Message | None  # completed·denied 면 마지막 assistant 메시지, max_turns 면 None
    messages: list[Message]  # 이번 run 까지의 전체 대화 (history + 새 메시지)
    llm_calls: int  # chat() 호출 횟수
    stop_reason: StopReason
    denial: Denial | None = None  # stop_reason="denied" 일 때만


@runtime_checkable
class AgentLoop(Protocol):
    async def run(
        self,
        user_input: str,
        principal: Principal,
        session_id: str,
        history: list[Message] | None = None,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> RunResult: ...


class HarnessFactory(Protocol):
    """각자 구현 패키지에서 `create_harness` 라는 이름으로 내보낸다."""

    def __call__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        checker: PermissionChecker,
        approver: ApprovalHandler,
        audit: AuditLogger,
        max_turns: int,
    ) -> AgentLoop: ...
