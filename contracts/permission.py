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
from typing import Any, Protocol, runtime_checkable


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class Principal:
    """누가 요청했는가. RBAC 판단의 기준."""

    user_id: str
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    """LLM 이 요청한 툴 호출 1건."""

    call_id: str  # LLM 이 준 tool_call id (결과를 돌려줄 때 짝 맞추기용)
    tool_name: str
    arguments: dict[str, Any]
    principal: Principal
    session_id: str


@dataclass(frozen=True)
class PermissionResult:
    decision: Decision
    reason: str = ""  # DENY/ASK 일 때 사람이 읽을 이유 (LLM·사용자에게 그대로 보여줌)
    rule_id: str | None = None  # 어떤 정책 규칙에 걸렸는지 (없으면 기본값 적용)


@runtime_checkable
class PermissionChecker(Protocol):
    async def check(self, call: ToolCall) -> PermissionResult: ...


@runtime_checkable
class ApprovalHandler(Protocol):
    """Decision.ASK 일 때 사용자에게 승인을 받는다. 승인되면 True.

    웹 UI 응답을 기다리는 동안 await 로 양보한다 (예: asyncio.Future 를 만들어 두고
    승인 API 가 set_result 하는 방식). 타임아웃 처리는 구현 쪽 책임.
    """

    async def request_approval(self, call: ToolCall, result: PermissionResult) -> bool: ...


@dataclass(frozen=True)
class AuditRecord:
    """권한 판정 1건의 기록. 감사 로그 한 줄(JSONL)에 해당."""

    call: ToolCall
    result: PermissionResult
    approved: bool | None = None  # ASK 였을 때 사용자 응답, 그 외엔 None
    executed: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@runtime_checkable
class AuditLogger(Protocol):
    async def log(self, record: AuditRecord) -> None: ...


# ── 공통 정책 규칙 (최소 형식) ───────────────────────────────
#
# 정책 파일 형식(YAML 등)은 각자 자유. 다만 공통 테스트를 돌리려면
# 아래 PolicyRule 목록을 받아 체커를 만들 수 있어야 한다.
# 매칭 규칙:
# - 목록 순서대로 보고 처음 맞는 규칙이 이긴다.
# - tool 이 "*" 이면 모든 툴에 맞는다.
# - roles 가 비어 있으면 모든 사용자, 아니면 principal.roles 중 하나라도 겹치면 맞는다.
# - 맞는 규칙이 없으면 default 결정, rule_id=None.
# - DENY / ASK 결과의 reason 은 비어 있으면 안 된다.


@dataclass(frozen=True)
class PolicyRule:
    id: str
    tool: str  # 툴 이름 또는 "*"
    decision: Decision
    roles: tuple[str, ...] = ()
    reason: str = ""


class CheckerFactory(Protocol):
    """각자 구현 패키지에서 `create_checker` 라는 이름으로 내보낸다."""

    def __call__(
        self, *, rules: list[PolicyRule], default: Decision
    ) -> PermissionChecker: ...


class AlwaysAllowChecker:
    """개발용 임시 구현. 권한 구현이 나오기 전까지 하네스 테스트에 쓴다."""

    async def check(self, call: ToolCall) -> PermissionResult:
        return PermissionResult(Decision.ALLOW, reason="dev: always allow")
