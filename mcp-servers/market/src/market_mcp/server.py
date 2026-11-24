"""시장 데이터 MCP 서버. 외부 API 접근과 인증키는 이 프로세스에만 둔다. 조회만 하고 저장하지 않는다."""

from __future__ import annotations

import os
import secrets
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
    server = MCPServer("market", version="0.1.0", instructions="ECOS 환율·기준금리 조회만 한다 (저장하지 않음). 보고서 작성·저장은 플랫폼이 한다. replay는 기록 재생이며 실시간이 아님.")

    @server.tool()
    async def market_snapshot(requester_id: Owner, base_date: str | None = None) -> dict:
        """기준일의 USD·JPY(100엔)·EUR·CNY와 기준금리. 생략하면 한국 시간 오늘. 직전 관측값 대비를 코드로 계산."""
        try:
            return await service.market_snapshot(requester_id, base_date)
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
    host, port = os.environ.get("MARKET_MCP_HOST", "127.0.0.1"), int(os.environ.get("MARKET_MCP_PORT", "8002"))
    service = MarketService(provider)  # 저장소 없음: 조회 결과는 플랫폼이 보관한다
    print(f"[market-mcp] mode={provider.mode} · http://{host}:{port}/mcp", flush=True)
    uvicorn.run(build_http_app(service, os.environ.get("MARKET_MCP_TOKEN"), host=host), host=host, port=port)


if __name__ == "__main__":
    main()
