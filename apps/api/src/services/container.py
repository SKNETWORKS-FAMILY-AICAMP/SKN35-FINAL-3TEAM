"""앱이 쓰는 구현 묶음. 서버가 뜰 때 한 번 만들고, 테스트에서는 가짜로 바꿔 끼운다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from contracts.harness import LLMClient
from contracts.permission import AuditLogger, Decision, PermissionChecker
from harness.llm import OpenAICompatLLM
from harness.local_math import LocalMathClient
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import load_policy
from services.agents import InMemoryAgentStore, InMemoryShareRequestStore
from services.sessions import InMemorySessionStore
from services.users import UserDirectory
from tools.arithmetic import arithmetic_tools
from tools.read_doc import ReadDocumentTool
from tools.registry import SimpleToolRegistry
from tools.search_doc import SearchDocumentsTool
from tools.today import TodayTool

REPO_ROOT = Path(__file__).resolve().parents[4]

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
    agents: InMemoryAgentStore = field(default_factory=InMemoryAgentStore)
    share_requests: InMemoryShareRequestStore = field(default_factory=InMemoryShareRequestStore)
    max_turns: int = 10
    tool_timeout: float | None = 30.0
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


def llm_from_env() -> LLMClient:
    """LLM_PROVIDER=openai (기본, Ollama 등 OpenAI 호환) | local_math (LLM 없이 사칙연산 데모)."""
    provider = os.environ.get("LLM_PROVIDER", "openai")
    if provider == "local_math":
        return LocalMathClient()
    if provider != "openai":
        raise ValueError(f"알 수 없는 LLM_PROVIDER: {provider}")
    return OpenAICompatLLM.from_env()


def build_tools(document_path: str | Path) -> list:
    """관리자가 등록한 툴 목록. 에이전트는 이 중에서 고른다 (MCP 연결 전까지는 코드로 등록)."""
    doc = Path(document_path)
    return [*arithmetic_tools(), SearchDocumentsTool(doc), ReadDocumentTool(doc), TodayTool()]


def build_from_env() -> AppServices:
    env = os.environ.get
    rules, default = load_policy(
        env("PERMISSION_POLICY_PATH", "config/permissions.yaml"),
        default=Decision(env("PERMISSION_DEFAULT_DECISION", "ask")),
    )
    return AppServices(
        llm=llm_from_env(),
        tools=SimpleToolRegistry(build_tools(env("DOCUMENT_CHUNKS_PATH", str(REPO_ROOT / "data/fixtures/document_chunks.json")))),
        checker=create_checker(rules=rules, default=default),
        approvals=FutureApprovalHandler(float(env("PERMISSION_APPROVAL_TIMEOUT_SECONDS", "120"))),
        audit=JsonlAuditLogger(env("PERMISSION_AUDIT_LOG_PATH", "logs/permission_audit.jsonl")),
        users=UserDirectory.from_yaml(env("DEV_USERS_PATH", "config/dev_users.yaml")),
        max_turns=int(env("HARNESS_MAX_TURNS", "10")),
        tool_timeout=float(env("HARNESS_TOOL_TIMEOUT_SECONDS", "30")),
    )
