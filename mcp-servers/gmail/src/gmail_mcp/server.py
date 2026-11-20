"""MCP 인터페이스 — 에이전트가 쓰는 메일 툴 3개. 로직은 service.py.

- 직원마다 자기 Google 계정을 연결한다 (gmail-mcp-auth --user <사번>).
- requester_id 는 LLM에게 보이지 않는다. 플랫폼이 로그인 사용자 사번으로 채운다 (manifest 의 inject).
- 메일은 관계형 DB 에만 저장되고, 검색 · 읽기는 요청자 본인 메일만 본다.
- 정기 동기화는 LLM 툴이 아니라 runner.py(gmail-sync)로 따로 실행한다.

실행:  uv run gmail-mcp      (GMAIL_ADAPTER=fake 면 가상 메일함, 기본은 직원 개인 Gmail)
"""

from __future__ import annotations

import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .accounts import factory
from .config import Settings
from .models import MailSearchResult, MailThread, SendResult
from .service import InvalidRequest, MailService
from .status_page import render_status_page
from .store import MailStore

RequesterId = Annotated[str, Field(description="요청자 사번 (플랫폼이 채움)")]


def is_simulated(service: MailService) -> bool:
    return service.settings.adapter == "fake"


def mode_label(service: MailService) -> str:
    if is_simulated(service):
        return "가상 메일함(fake) — 실제 Gmail이 아니며 메일이 발송되지 않음"
    return "직원 개인 Gmail 계정"


def build_server(service: MailService) -> MCPServer:
    mode = mode_label(service)
    mcp = MCPServer("gmail", version="0.2.0", instructions=f"[{mode}] 내 메일 검색·읽기, 내 계정으로 메일 발송")

    @mcp.tool()
    async def search_mail(
        requester_id: RequesterId,
        query: Annotated[str, Field(description="찾을 단어 (제목·보낸 사람·본문). 여러 단어는 모두 포함. 비우면 최근 메일")] = "",
        max_results: Annotated[int, Field(description="최대 결과 수 (1~20)")] = 10,
    ) -> MailSearchResult:
        """내 메일함에서 메일을 검색해 제목·보낸 사람·날짜 목록을 돌려준다. 검색 전에 새 메일을 먼저 가져온다."""
        try:
            return await service.search_mail(requester_id, query, max_results)
        except InvalidRequest as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def read_mail(
        requester_id: RequesterId,
        thread_id: Annotated[str, Field(description="search_mail 결과의 thread_id")],
    ) -> MailThread:
        """내 메일함의 메일 스레드 본문을 읽는다."""
        try:
            return await service.read_mail(requester_id, thread_id)
        except InvalidRequest as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def send_mail(
        requester_id: RequesterId,
        to: Annotated[list[str], Field(description="받는 사람 이메일 목록")],
        subject: Annotated[str, Field(description="제목 (한 줄, 200자 이내)")],
        body: Annotated[str, Field(description="본문 (일반 텍스트)")],
        cc: Annotated[list[str] | None, Field(description="참조 이메일 목록 (선택)")] = None,
    ) -> SendResult:
        """내 메일 계정으로 메일을 보낸다. 본문 끝에 사내 AI 에이전트를 통해 보냈다는 안내가 붙는다."""
        try:
            return await service.send_mail(requester_id, to, subject, body, cc)
        except InvalidRequest as exc:
            raise ToolError(str(exc)) from exc

    @mcp.custom_route("/", methods=["GET"])
    async def status_page(request: Request) -> HTMLResponse:
        tools = [(t.name, t.description or "") for t in await mcp.list_tools()]
        return HTMLResponse(render_status_page(
            name="gmail", mode=mode, simulated=is_simulated(service),
            mcp_url=f"{str(request.base_url).rstrip('/')}/mcp", tools=tools,
        ))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "server": "gmail",
            "adapter": "fake" if is_simulated(service) else "google",
            "mode": mode,
            "connected_accounts": len(await service.store.connected_users()),
        })

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = MailService(MailStore(settings.db_path), factory(settings), settings)
    # 어떤 메일함에 붙었는지 시작할 때 분명히 보여준다 (환경변수가 터미널에 남아 있어 헷갈리는 경우 방지)
    print(f"[gmail-mcp] 모드: {mode_label(service)}  (GMAIL_ADAPTER={settings.adapter})", file=sys.stderr, flush=True)
    print(f"[gmail-mcp] 메일 DB: {settings.db_path}", file=sys.stderr, flush=True)
    print(f"[gmail-mcp] 주소: http://{settings.host}:{settings.port}/mcp", file=sys.stderr, flush=True)
    server = build_server(service)
    server.run("streamable-http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
