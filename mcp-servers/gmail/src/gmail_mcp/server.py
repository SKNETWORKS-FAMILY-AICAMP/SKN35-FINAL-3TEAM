"""MCP 인터페이스 — 에이전트가 쓰는 메일 툴 3개. 로직은 service.py.

- requester_id · requester_email 은 LLM에게 보이지 않는다. 플랫폼이 로그인 사용자로 채운다 (manifest 의 inject).
- 동기화(증분 · 권한)는 LLM 툴이 아니라 runner.py(gmail-sync)로 따로 실행한다.

실행:  uv run gmail-mcp      (GMAIL_ADAPTER=fake 면 가상 메일함, 기본은 팀 Gmail 계정)
"""

from __future__ import annotations

import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .client import build_client
from .config import Settings
from .models import MailSearchResult, MailThread, SendResult
from .service import InvalidRequest, MailService
from .status_page import render_status_page

RequesterEmail = Annotated[str, Field(description="요청자 이메일 (플랫폼이 채움)")]


def mode_label(service: MailService) -> str:
    if getattr(service.client, "simulated", False):
        return "가상 메일함(fake) — 실제 Gmail이 아니며 메일이 발송되지 않음"
    return "팀 Gmail 계정"


def build_server(service: MailService) -> MCPServer:
    mode = mode_label(service)
    mcp = MCPServer("gmail", version="0.1.0", instructions=f"[{mode}] 팀 메일함: 내가 참여한 메일 검색·읽기, 메일 발송")

    @mcp.tool()
    async def search_mail(
        requester_email: RequesterEmail,
        query: Annotated[str, Field(description="찾을 단어 또는 Gmail 검색 문법 (예: 경비 정산, newer_than:7d). 비우면 최근 메일")] = "",
        max_results: Annotated[int, Field(description="최대 결과 수 (1~20)")] = 10,
    ) -> MailSearchResult:
        """내가 보내거나 받은(참조 포함) 팀 메일함의 메일을 검색해 제목·보낸 사람·날짜 목록을 돌려준다."""
        try:
            return await service.search_mail(requester_email, query, max_results)
        except InvalidRequest as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def read_mail(
        requester_email: RequesterEmail,
        thread_id: Annotated[str, Field(description="search_mail 결과의 thread_id")],
    ) -> MailThread:
        """메일 스레드의 본문을 읽는다. 내가 참여한 메일만 열 수 있다."""
        try:
            return await service.read_mail(requester_email, thread_id)
        except InvalidRequest as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def send_mail(
        requester_id: Annotated[str, Field(description="요청자 사번 (플랫폼이 채움)")],
        requester_email: RequesterEmail,
        to: Annotated[list[str], Field(description="받는 사람 이메일 목록")],
        subject: Annotated[str, Field(description="제목 (한 줄, 200자 이내)")],
        body: Annotated[str, Field(description="본문 (일반 텍스트)")],
        cc: Annotated[list[str] | None, Field(description="참조 이메일 목록 (선택)")] = None,
    ) -> SendResult:
        """팀 메일 계정으로 메일을 보낸다. 답장은 요청한 직원에게 가도록 설정되고, 본문 끝에 요청자 안내가 붙는다."""
        try:
            return await service.send_mail(requester_id, requester_email, to, subject, body, cc)
        except InvalidRequest as exc:
            raise ToolError(str(exc)) from exc

    @mcp.custom_route("/", methods=["GET"])
    async def status_page(request: Request) -> HTMLResponse:
        tools = [(t.name, t.description or "") for t in await mcp.list_tools()]
        return HTMLResponse(render_status_page(
            name="gmail", mode=mode, simulated=getattr(service.client, "simulated", False),
            mcp_url=f"{str(request.base_url).rstrip('/')}/mcp", tools=tools,
        ))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "server": "gmail",
            "adapter": "fake" if getattr(service.client, "simulated", False) else "google",
            "mode": mode,
        })

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = MailService(build_client(settings), settings)
    # 어떤 메일함에 붙었는지 시작할 때 분명히 보여준다 (환경변수가 터미널에 남아 있어 헷갈리는 경우 방지)
    print(f"[gmail-mcp] 모드: {mode_label(service)}  (GMAIL_ADAPTER={settings.adapter})", file=sys.stderr, flush=True)
    print(f"[gmail-mcp] 주소: http://{settings.host}:{settings.port}/mcp", file=sys.stderr, flush=True)
    server = build_server(service)
    server.run("streamable-http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
