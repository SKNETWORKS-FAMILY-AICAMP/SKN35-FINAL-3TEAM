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
from services.uploads import UploadStore
from tools.arithmetic import arithmetic_tools
from tools.parse_document import DEFAULT_TIMEOUT as DEFAULT_PARSE_TIMEOUT
from tools.parse_document import ParseDocumentTool
from tools.registry import SimpleToolRegistry
from tools.read_doc import ReadDocumentTool
from tools.get_review_case import GetReviewCaseTool
from tools.submit_review_request import SubmitReviewRequestTool
from tools.review_store import ReviewStore
from tools.search_doc import SearchDocumentsTool
from tools.today import TodayTool
from tools.gmail import build_gmail_tools

REPO_ROOT = Path(__file__).resolve().parents[4]

# 모든 에이전트 앞에 붙는 공통 규칙. 에이전트별 역할 지시는 services/chat.py 에서 뒤에 붙인다.
DEFAULT_SYSTEM_PROMPT = """너는 은행 직원의 업무를 돕는 사내 에이전트다. 답을 읽는 사람은 일반 직원이다.

[답하는 방식]
- 한국어 존댓말로, 결론부터 한두 문장으로 말한다. 그다음 지켜야 할 내용을 짧은 목록으로 정리한다.
- 문서를 근거로 답했으면 마지막 줄에 "근거:" 다음에 문서 이름과 조항을 쓴다. 예) 근거: 영업점 서류 처리 공통 지침 제2절 1항
- 툴 이름, 문서 ID, 문단 ID(chunk_id), JSON 같은 내부 값은 답에 절대 쓰지 않는다.

[툴 쓰는 법]
- 사용자는 툴을 직접 쓸 수 없다. "툴을 사용해 주세요" 같은 말로 넘기지 말고, 더 확인할 게 있으면 네가 직접 툴을 불러 확인한 뒤 답한다.
- 사내 문서를 검색했는데 결과에 본문이 없으면, 관련 있어 보이는 문단의 원문을 직접 열어 읽고 나서 답한다.
- 문서에 없는 내용은 지어내지 않는다. 찾지 못했으면 찾지 못했다고 말한다.
- 권한이 없다는 결과를 받으면 다른 방법으로 우회하지 말고 이유를 알려 준다.

아래에 이 에이전트의 역할 지시가 이어질 수 있다. 역할 지시와 위 규칙이 부딪히면 위 규칙을 따른다."""


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
    uploads: UploadStore = field(default_factory=lambda: UploadStore(REPO_ROOT / "data/uploads"))
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


def build_tools(document_path: str | Path, upload_dir: str | Path | None = None,
                parse_timeout: float | None = DEFAULT_PARSE_TIMEOUT,
                holiday_path: Path | None = None, review_store: ReviewStore | None = None,
                upload_store: UploadStore | None = None) -> list:
    """로컬 도구와 환경변수로 활성화한 Gmail MCP 도구를 함께 등록한다."""
    doc = Path(document_path)
    uploads = Path(upload_dir) if upload_dir else REPO_ROOT / "data/uploads"
    upload_store = upload_store or UploadStore(uploads)
    review_store = review_store or ReviewStore(
        REPO_ROOT / "data/fixtures/review_cases.json",
        Path(os.environ.get("REVIEW_REQUEST_DB_PATH", str(REPO_ROOT / "data/review_requests.sqlite3"))),
    )
    return [
        *arithmetic_tools(), SearchDocumentsTool(doc), ReadDocumentTool(doc), TodayTool(holiday_path),
        ParseDocumentTool(uploads, timeout=parse_timeout, uploads=upload_store),
        GetReviewCaseTool(review_store), SubmitReviewRequestTool(review_store),
        *build_gmail_tools(),
    ]


def build_from_env() -> AppServices:
    env = os.environ.get
    rules, default = load_policy(
        env("PERMISSION_POLICY_PATH", "config/permissions.yaml"),
        default=Decision(env("PERMISSION_DEFAULT_DECISION", "ask")),
    )
    repository_root = Path(__file__).resolve().parents[4]
    document_path = env("DOCUMENT_CHUNKS_PATH", str(repository_root / "data/fixtures/document_chunks.json"))
    holiday_path = repository_root / "data" / "fixtures" / "business_holidays_demo.json"
    uploads = UploadStore(Path(env("UPLOAD_DIR", str(REPO_ROOT / "data/uploads"))))
    review_store = ReviewStore(
        repository_root / "data" / "fixtures" / "review_cases.json",
        Path(env("REVIEW_REQUEST_DB_PATH", str(repository_root / "data" / "review_requests.sqlite3"))),
    )
    return AppServices(
        llm=llm_from_env(),
        tools=SimpleToolRegistry(build_tools(
            document_path, upload_dir=env("UPLOAD_DIR"),
            parse_timeout=float(env("PARSE_TOOL_TIMEOUT_SECONDS", str(DEFAULT_PARSE_TIMEOUT))),
            holiday_path=holiday_path, review_store=review_store, upload_store=uploads,
        )),
        uploads=uploads,
        checker=create_checker(rules=rules, default=default),
        approvals=FutureApprovalHandler(float(env("PERMISSION_APPROVAL_TIMEOUT_SECONDS", "120"))),
        audit=JsonlAuditLogger(env("PERMISSION_AUDIT_LOG_PATH", "logs/permission_audit.jsonl")),
        users=UserDirectory.from_yaml(env("DEV_USERS_PATH", "config/dev_users.yaml")),
        max_turns=int(env("HARNESS_MAX_TURNS", "10")),
        tool_timeout=float(env("HARNESS_TOOL_TIMEOUT_SECONDS", "30")),
    )
