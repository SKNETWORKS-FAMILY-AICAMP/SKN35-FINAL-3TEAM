"""권한 계약 (하네스 ↔ 권한).
하네스는 툴을 실행하기 직전에 반드시 await PermissionChecker.check() 를 호출한다.
모든 프로토콜 메서드는 async 다 (정책·감사 저장소가 DB/네트워크일 수 있으므로).
- ALLOW: 바로 실행
- DENY : 실행하지 않고, reason 을 툴 결과(에러)로 LLM 에게 돌려준다
- ASK  : ApprovalHandler 로 사용자 승인을 받은 뒤 결과에 따라 실행/거부

권한 쪽은 이 파일의 타입만 보고 구현하고, 하네스 쪽은 구현을 몰라도 된다.
여기를 바꾸려면 하네스·권한 담당 둘 다 동의해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class Principal:
    """누가 요청했는가. ABAC 판단의 기준 (역할 + 속성).

    attributes 표준 키 (ERD users 테이블과 같은 이름):
      dept_id  소속 부서 ID
      position       직급
      security_grade 보안등급 (int, 클수록 높음)
    필요하면 키를 더 넣어도 된다. 값은 로그인 시점에 서버가 채우며 LLM 이 바꿀 수 없다.
    """

    user_id: str
    roles: tuple[str, ...] = ()
    # dict 라서 해시에서는 뺀다 (Principal 자체는 계속 해시 가능)
    attributes: dict[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class ToolCall:
    """LLM 이 요청한 툴 호출 1건."""

    call_id: str  # LLM 이 준 tool_call id (결과를 돌려줄 때 짝 맞추기용)
    tool_name: str
    arguments: dict[str, Any]
    principal: Principal
    session_id: str
    agent_id: str | None = None  # 어떤 에이전트로 실행 중인지 (None = 기본 에이전트). 감사 로그용
    run_id: str | None = None  # 어떤 요청(agent_run) 안의 호출인지


@dataclass(frozen=True)
class PermissionResult:
    decision: Decision
    reason: str = ""  # DENY/ASK 일 때 사람이 읽을 이유 (LLM·사용자에게 그대로 보여줌)
    rule_id: str | None = None  # 어떤 정책 규칙에 걸렸는지 (없으면 기본값 적용)


@runtime_checkable
class PermissionChecker(Protocol):
    async def check(self, call: ToolCall) -> PermissionResult: ...

# 승인 요청의 결과 (DB approval_request.status 와 같은 값)
#   approved  사용자가 승인함 → 실행
#   rejected  사용자가 거절함
#   timeout   정해진 시간 안에 응답이 없음
ApprovalOutcome = Literal["approved", "rejected", "timeout"]


@runtime_checkable
class ApprovalHandler(Protocol):
    """Decision.ASK 일 때 사용자에게 승인을 받는다.

    웹 UI 응답을 기다리는 동안 await 로 양보한다 (예: asyncio.Future 를 만들어 두고
    승인 API 가 set_result 하는 방식). 시간 제한은 구현 쪽 책임이며, 시간이 지나면
    예외 대신 "timeout" 을 돌려준다.
    """

    async def request_approval(self, call: ToolCall, result: PermissionResult) -> ApprovalOutcome: ...


@dataclass(frozen=True)
class AuditRecord:
    """권한 판정 1건의 기록. 감사 로그 한 줄(JSONL)에 해당."""

    call: ToolCall
    result: PermissionResult
    approval: ApprovalOutcome | None = None  # ASK 였을 때 승인 결과, 그 외엔 None
    executed: bool = False
    tool_ok: bool | None = None  # executed 일 때 툴이 성공(ToolResult.ok)했는지. 실행 안 했으면 None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tool_ok: bool | None = None


@runtime_checkable
class AuditLogger(Protocol):
    async def log(self, record: AuditRecord) -> None: ...


# ── 공통 정책 규칙 (ABAC) ───────────────────────────────────
#
# 정책 파일 형식(YAML 등)은 각자 자유. 다만 공통 테스트를 돌리려면
# 아래 PolicyRule 목록을 받아 체커를 만들 수 있어야 한다.
# 매칭 규칙:
# - 목록 순서대로 보고 처음 맞는 규칙이 이긴다.
# - tool 이 "*" 이면 모든 툴에 맞는다.
# - roles 가 비어 있으면 모든 사용자, 아니면 principal.roles 중 하나라도 겹치면 맞는다.
# - conditions 는 모두(AND) 참이어야 맞는다. 비어 있으면 조건 없음.
# - 맞는 규칙이 없으면 default 결정, rule_id=None.
# - DENY / ASK 결과의 reason 은 비어 있으면 안 된다.
#
# Condition 평가:
# - attr / value_from 은 "principal.<키>" 또는 "args.<키>" 경로다.
#     principal.user_id, principal.roles, principal.<attributes 의 키>
#     args.<툴 인자 키>  (점으로 중첩 dict 를 따라간다: args.target.dept_id)
# - 비교 대상은 value_from 이 있으면 그 경로의 값, 없으면 value.
# - attr 또는 value_from 경로에 값이 없으면 그 조건은 거짓이다.
#   (그래서 DENY 규칙이 속성 누락으로 빠져나갈 수 있다. 위험한 툴은 default 를 deny/ask 로 둘 것)
# - 타입이 안 맞아 비교할 수 없으면 (예: "3" > 2) 예외 없이 거짓이다.
# - op:
#     eq, ne                 같다 / 다르다
#     gt, gte, lt, lte       크기 비교 (숫자)
#     in, not_in             값이 비교 대상(리스트) 안에 있다 / 없다
#     contains               값(리스트·문자열)이 비교 대상을 포함한다


ConditionOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains"]


@dataclass(frozen=True)
class Condition:
    attr: str
    op: ConditionOp
    value: Any = None
    value_from: str | None = None  # 다른 경로 값과 비교할 때 (예: "principal.dept_id")


@dataclass(frozen=True)
class PolicyRule:
    id: str
    tool: str  # 툴 이름 또는 "*"
    decision: Decision
    roles: tuple[str, ...] = ()
    reason: str = ""
    conditions: tuple[Condition, ...] = ()


class CheckerFactory(Protocol):
    """각자 구현 패키지에서 `create_checker` 라는 이름으로 내보낸다."""

    def __call__(
        self, *, rules: list[PolicyRule], default: Decision
    ) -> PermissionChecker: ...


class AlwaysAllowChecker:
    """개발용 임시 구현. 권한 구현이 나오기 전까지 하네스 테스트에 쓴다."""

    async def check(self, call: ToolCall) -> PermissionResult:
        return PermissionResult(Decision.ALLOW, reason="dev: always allow")
