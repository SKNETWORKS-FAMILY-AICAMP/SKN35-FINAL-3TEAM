"""앱이 쓰는 구현 묶음. 서버가 뜰 때 한 번 만들고, 테스트에서는 가짜로 바꿔 끼운다."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from openai import NotFoundError

from contracts.harness import LLMClient
from contracts.permission import AuditLogger, Decision, PermissionChecker
from harness.grounding import RequireSearch
from harness.llm import OpenAICompatLLM
from harness.local_math import LocalMathClient
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import load_policy
from services.agents import InMemoryAgentStore, InMemoryShareRequestStore
from services.mail_accounts import GmailAccountResolver
from services.sessions import InMemorySessionStore
from services.templates import InMemoryTemplateStore, load_templates
from services.users import UserDirectory
from services.uploads import UploadStore
from tools.arithmetic import arithmetic_tools
from tools.parse_doc import DEFAULT_TIMEOUT as DEFAULT_PARSE_TIMEOUT
from tools.parse_doc import ParseDocTool
from tools.registry import SimpleToolRegistry
from tools.read_doc import ReadDocTool
from tools.get_review_case import GetReviewCaseTool
from tools.review_store import ReviewStore
from tools.search_doc import SearchDocsTool
from tools.submit_review_request import SubmitReviewRequestTool
from tools.today import TodayTool
from tools.gmail import build_gmail_tools
from tools.market import build_market_tools

REPO_ROOT = Path(__file__).resolve().parents[4]
log = logging.getLogger("uvicorn.error")

# 모든 에이전트 앞에 붙는 공통 규칙. 에이전트별 역할 지시는 services/chat.py 에서 뒤에 붙인다.
DEFAULT_SYSTEM_PROMPT = """너는 은행 직원의 업무를 돕는 사내 에이전트다. 답을 읽는 사람은 일반 직원이다.

[답하는 방식]
- 한국어 존댓말로, 결론부터 한두 문장으로 말한다. 그다음 지켜야 할 내용을 짧은 목록으로 정리한다.
- "근거:" 줄은 이번에 사내 문서를 검색하거나 열어서 실제로 확인한 경우에만 마지막 줄에 쓴다.
  확인한 문서의 이름과 조항을 결과에 나온 그대로 쓴다. 예) 근거: 영업점 서류 처리 공통 지침 제2절 1항
- 심사 건 조회, 날짜 계산처럼 문서가 아닌 결과로 답했으면 "근거:" 줄을 쓰지 않는다.
- 확인하지 않은 문서 이름이나 조항 번호는 절대 쓰지 않는다.
- 툴 이름, 함수 이름, 문서 ID, 문단 ID(chunk_id), JSON 같은 내부 값은 답에 절대 쓰지 않는다.

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
    templates: InMemoryTemplateStore = field(default_factory=InMemoryTemplateStore)
    draft_llm: LLMClient | None = None  # 에이전트 초안 전용 (더 큰 모델). 없으면 llm 을 쓴다
    max_turns: int = 10
    tool_timeout: float | None = 30.0
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    require_search: RequireSearch | None = field(default_factory=RequireSearch)  # 문서 질문 검색 강제. None 이면 끔
    require_review_lookup: bool = True  # 심사 건의 상태는 실제 조회 결과로만 안내


def llm_from_env() -> LLMClient:
    """LLM_PROVIDER=openai (기본, Ollama 등 OpenAI 호환) | local_math (LLM 없이 사칙연산 데모)."""
    provider = os.environ.get("LLM_PROVIDER", "openai")
    if provider == "local_math":
        return LocalMathClient()
    if provider != "openai":
        raise ValueError(f"알 수 없는 LLM_PROVIDER: {provider}")
    if os.environ.get("LLM_MODEL"):
        return OpenAICompatLLM.from_env()
    # 기본: 컨텍스트를 늘린 qwen3:8b-24k (config/ollama/README.md). 아직 안 만들었으면 qwen3:8b 로 대신한다
    return FallbackLLM(OpenAICompatLLM.from_env(model=CHAT_MODEL), OpenAICompatLLM.from_env(model=CHAT_MODEL_FALLBACK))


CHAT_MODEL = "qwen3:8b-24k"
CHAT_MODEL_FALLBACK = "qwen3:8b"


class FallbackLLM:
    """먼저 primary 를 부르고, 그 모델이 서버에 없으면(404) fallback 으로 대신한다.

    초안 전용 큰 모델(qwen3:14b)이나 컨텍스트를 늘린 채팅 모델(qwen3:8b-24k)을
    아직 만들지 않은 팀원도 그대로 돌릴 수 있게 하려는 것.
    연결 실패·타임아웃은 대신하지 않고 그대로 올린다 (느린 것과 없는 것은 다르다).
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self.primary, self.fallback = primary, fallback
        self._missing = False

    async def chat(self, messages, tools):
        if not self._missing:
            try:
                return await self.primary.chat(messages, tools)
            except NotFoundError:
                self._missing = True
                log.warning("모델 %s 이 없어 %s 로 대신합니다 (config/ollama/README.md 참고)",
                            getattr(self.primary, "model", "?"), getattr(self.fallback, "model", "?"))
        return await self.fallback.chat(messages, tools)

    async def chat_required_tool(self, messages, tool):
        """둘 다 툴 호출 강제를 지원할 때만 쓴다 (RequiredToolLLMClient)."""
        if not self._missing:
            try:
                return await self.primary.chat_required_tool(messages, tool)
            except NotFoundError:
                self._missing = True
        return await self.fallback.chat_required_tool(messages, tool)


def draft_llm_from_env(chat_llm: LLMClient) -> LLMClient:
    """DRAFT_LLM_MODEL (기본 qwen3:14b) 로 초안 전용 LLM 을 만든다. 비우면 채팅 모델을 그대로 쓴다."""
    env = os.environ.get
    model = env("DRAFT_LLM_MODEL", "qwen3:14b").strip()
    if env("LLM_PROVIDER", "openai") != "openai" or not model or model == env("LLM_MODEL", "qwen3:8b"):
        return chat_llm
    primary = OpenAICompatLLM(
        base_url=env("DRAFT_LLM_BASE_URL") or env("LLM_BASE_URL", "http://localhost:11434/v1"),
        model=model,
        api_key=env("DRAFT_LLM_API_KEY") or env("LLM_API_KEY", "ollama"),
        timeout=float(env("DRAFT_LLM_TIMEOUT_SECONDS", "300")),
    )
    return FallbackLLM(primary, chat_llm)


FIXTURES = REPO_ROOT / "data/fixtures"


def build_tools(doc_path: str | Path = FIXTURES / "doc_chunks.json", upload_dir: str | Path | None = None,
                parse_timeout: float | None = DEFAULT_PARSE_TIMEOUT,
                holiday_path: str | Path | None = FIXTURES / "business_holidays_demo.json", review_store: ReviewStore | None = None,
                upload_store: UploadStore | None = None, doc_tools: list | None = None,
                gmail_account_resolver: GmailAccountResolver | None = None) -> list:
    """로컬 도구와 환경변수로 활성화한 Gmail MCP 도구를 함께 등록한다.
    doc_tools 를 주면 파일 예시 대신 그 문서 검색·열람 도구를 쓴다 (DOC_STORE=db → tools/db_docs.py)."""
    doc = Path(doc_path)
    uploads = Path(upload_dir) if upload_dir else REPO_ROOT / "data/uploads"
    upload_store = upload_store or UploadStore(uploads)
    review_store = review_store or ReviewStore(
        REPO_ROOT / "data/fixtures/review_cases.json",
        Path(os.environ.get("REVIEW_REQUEST_DB_PATH", str(REPO_ROOT / "data/review_requests.sqlite3"))),
    )
    return [
        *arithmetic_tools(), *(doc_tools or [SearchDocsTool(doc), ReadDocTool(doc)]),
        TodayTool(Path(holiday_path) if holiday_path is not None else None),
        ParseDocTool(uploads, timeout=parse_timeout, uploads=upload_store),
        GetReviewCaseTool(review_store), SubmitReviewRequestTool(review_store),
        *build_gmail_tools(account_resolver=gmail_account_resolver),
        *build_market_tools(),
    ]


def db_doc_tools_from_env() -> list | None:
    """DOC_STORE=db 이면 팀 DB(pgvector) 문서 도구, 아니면 None (파일 예시를 읽는 기존 도구).
    필요한 값: DATABASE_URL, EMBEDDING_MODEL_PATH(KURE-v3 폴더). 모델은 서버가 뜰 때 미리 올린다."""
    if os.environ.get("DOC_STORE", "file").strip().lower() != "db":
        return None
    from knowledge.ingestion.embedders import KureEmbedder
    from tools.db_docs import BackgroundEmbedder, DbReadDocTool, DbSearchDocsTool

    embedder = BackgroundEmbedder(lambda: KureEmbedder())
    log.info("문서 도구: 팀 DB(pgvector) 사용 — KURE-v3 를 미리 불러오는 중")
    return [DbSearchDocsTool(embedder), DbReadDocTool()]


def build_from_env(*, gmail_account_resolver: GmailAccountResolver | None = None) -> AppServices:
    env = os.environ.get
    rules, default = load_policy(
        env("PERMISSION_POLICY_PATH", "config/permissions.yaml"),
        default=Decision(env("PERMISSION_DEFAULT_DECISION", "ask")),
    )
    doc_path = env("DOC_CHUNKS_PATH", str(REPO_ROOT / "data/fixtures/doc_chunks.json"))
    holiday_path = Path(env("BUSINESS_HOLIDAYS_PATH", str(FIXTURES / "business_holidays_demo.json")))
    uploads = UploadStore(Path(env("UPLOAD_DIR", str(REPO_ROOT / "data/uploads"))))
    review_store = ReviewStore(
        Path(env("REVIEW_CASES_PATH", str(FIXTURES / "review_cases.json"))),
        Path(env("REVIEW_REQUEST_DB_PATH", str(REPO_ROOT / "data" / "review_requests.sqlite3"))),
    )
    llm = llm_from_env()
    return AppServices(
        llm=llm,
        draft_llm=draft_llm_from_env(llm),
        tools=SimpleToolRegistry(build_tools(
            doc_path, upload_dir=env("UPLOAD_DIR"),
            parse_timeout=float(env("PARSE_TOOL_TIMEOUT_SECONDS", str(DEFAULT_PARSE_TIMEOUT))),
            holiday_path=holiday_path, review_store=review_store, upload_store=uploads,
            doc_tools=db_doc_tools_from_env(),
            gmail_account_resolver=gmail_account_resolver,
        )),
        uploads=uploads,
        checker=create_checker(rules=rules, default=default),
        approvals=FutureApprovalHandler(float(env("PERMISSION_APPROVAL_TIMEOUT_SECONDS", "120"))),
        audit=JsonlAuditLogger(env("PERMISSION_AUDIT_LOG_PATH", "logs/permission_audit.jsonl")),
        users=UserDirectory.from_yaml(env("DEV_USERS_PATH", "config/dev_users.yaml")),
        templates=InMemoryTemplateStore(load_templates(env("AGENT_TEMPLATES_PATH", "config/agent_templates.yaml"))),
        max_turns=int(env("HARNESS_MAX_TURNS", "10")),
        tool_timeout=float(env("HARNESS_TOOL_TIMEOUT_SECONDS", "30")),
    )
