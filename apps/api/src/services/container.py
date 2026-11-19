"""앱이 쓰는 구현 묶음. 서버가 뜰 때 한 번 만들고, 테스트에서는 가짜로 바꿔 끼운다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from contracts.harness import LLMClient
from contracts.permission import AuditLogger, Decision, PermissionChecker
from harness.son.llm import OpenAICompatLLM
from permission.son import create_checker
from permission.son.approval import FutureApprovalHandler
from permission.son.audit import JsonlAuditLogger
from permission.son.policy import load_policy
from services.sessions import InMemorySessionStore
from services.users import UserDirectory
from tools.arithmetic import arithmetic_tools
from tools.registry import SimpleToolRegistry

DEFAULT_SYSTEM_PROMPT = (
    "너는 사내 업무를 돕는 에이전트다. 한국어로 간결하게 답한다. "
    "필요하면 제공된 툴을 쓰고, 권한이 없다는 결과를 받으면 우회하지 말고 사용자에게 이유를 알려준다."
)


@dataclass
class AppServices:
    llm: LLMClient
    tools: SimpleToolRegistry
    checker: PermissionChecker
    approvals: FutureApprovalHandler
    audit: AuditLogger
    users: UserDirectory
    sessions: InMemorySessionStore = field(default_factory=InMemorySessionStore)
    max_turns: int = 10
    tool_timeout: float | None = 30.0
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


def build_from_env() -> AppServices:
    env = os.environ.get
    rules, default = load_policy(
        env("PERMISSION_POLICY_PATH", "config/permissions.yaml"),
        default=Decision(env("PERMISSION_DEFAULT_DECISION", "ask")),
    )
    return AppServices(
        llm=OpenAICompatLLM.from_env(),
        tools=SimpleToolRegistry(arithmetic_tools()),
        checker=create_checker(rules=rules, default=default),
        approvals=FutureApprovalHandler(float(env("PERMISSION_APPROVAL_TIMEOUT_SECONDS", "120"))),
        audit=JsonlAuditLogger(env("PERMISSION_AUDIT_LOG_PATH", "logs/permission_audit.jsonl")),
        users=UserDirectory.from_yaml(env("DEV_USERS_PATH", "config/dev_users.yaml")),
        max_turns=int(env("HARNESS_MAX_TURNS", "10")),
        tool_timeout=float(env("HARNESS_TOOL_TIMEOUT_SECONDS", "30")),
    )
