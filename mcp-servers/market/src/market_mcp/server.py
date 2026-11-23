"""시장 데이터 MCP 서버. 외부 API 접근과 인증키는 이 프로세스에만 둔다."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .ecos import MarketError, provider_from_env
from .service import MarketService

Owner = Annotated[str, Field(min_length=1, max_length=64, description="플랫폼이 로그인 사용자 ID로 채움")]


def build_server(service: MarketService) -> MCPServer:
    server = MCPServer("market", version="0.1.0", instructions="보고서 양식 검색·전체 읽기, ECOS 환율·기준금리 조회, 선택한 양식으로 저장. replay는 기록 재생이며 실시간이 아님.")

    @server.tool()
    async def search_report_templates(requester_id: Owner, query: str = "") -> dict:
        """업무 목적·양식명으로 보고서 양식 후보를 검색한다. 검색 결과는 전체 양식이 아니다."""
        try:
            return await service.search_report_templates(requester_id, query)
        except MarketError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def get_report_template(requester_id: Owner, template_id: str, expected_hash: str | None = None) -> dict:
        """선택한 양식 전체와 필수 항목을 읽고, 저장할 때 쓸 고정된 양식 선택 ID를 반환한다."""
        try:
            return await service.get_report_template(requester_id, template_id, expected_hash)
        except MarketError as exc:
            raise ToolError(str(exc)) from exc
        except OSError:
            raise ToolError("양식 선택 정보를 보관할 수 없습니다.") from None

    @server.tool()
    async def market_snapshot(requester_id: Owner, base_date: str | None = None) -> dict:
        """기준일의 USD·JPY(100엔)·EUR·CNY와 기준금리. 생략하면 한국 시간 오늘. 직전 관측값 대비를 코드로 계산."""
        try:
            return await service.market_snapshot(requester_id, base_date)
        except MarketError as exc:
            raise ToolError(str(exc)) from exc
        except OSError:
            raise ToolError("시장 자료를 저장할 수 없습니다.") from None

    @server.tool()
    async def save_morning_report(requester_id: Owner, snapshot_id: str, summary: str = "",
                                  template_selection_id: str | None = None) -> dict:
        """본인 조회 결과의 snapshot_id와 요약만 받는다. 표를 서버 자료로 만들고 요약 검사 실패 시 표만 저장."""
        try:
            return await service.save_morning_report(requester_id, snapshot_id, summary, template_selection_id)
        except MarketError as exc:
            raise ToolError(str(exc)) from exc
        except OSError:
            raise ToolError("리포트를 저장할 수 없습니다.") from None

    @server.tool()
    async def get_morning_report(requester_id: Owner, report_id: str) -> dict:
        """보고서 번호로 본인이 저장한 리포트를 다시 읽는다."""
        try:
            return await service.get_morning_report(requester_id, report_id)
        except MarketError as exc:
            raise ToolError(str(exc)) from exc

    @server.custom_route("/health", methods=["GET"])
    async def health(_):
        return JSONResponse({"status": "ok", "server": "market", "mode": service.provider.mode,
                             "replay": service.provider.mode == "replay"})

    return server


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token):
        super().__init__(app)
        self.expected = ("Bearer " + token).encode()

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/mcp") and not secrets.compare_digest(
                request.headers.get("authorization", "").encode(), self.expected):
            return JSONResponse({"detail": "MCP 인증 토큰이 필요합니다"}, status_code=401)
        return await call_next(request)


def build_http_app(service: MarketService, token: str | None = None, *, host: str = "127.0.0.1"):
    app = build_server(service).streamable_http_app(host=host)
    if token:
        app.add_middleware(BearerTokenMiddleware, token=token)
    return app


def main():
    import uvicorn

    provider = provider_from_env()
    root = Path(os.environ.get("MARKET_STORAGE_ROOT", "data/storage/market"))
    host, port = os.environ.get("MARKET_MCP_HOST", "127.0.0.1"), int(os.environ.get("MARKET_MCP_PORT", "8002"))
    service = MarketService(provider, root)
    print(f"[market-mcp] mode={provider.mode} · http://{host}:{port}/mcp", flush=True)
    uvicorn.run(build_http_app(service, os.environ.get("MARKET_MCP_TOKEN"), host=host), host=host, port=port)


if __name__ == "__main__":
    main()
