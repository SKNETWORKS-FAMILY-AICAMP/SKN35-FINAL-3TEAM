"""시장 데이터 MCP 서버. 외부 API(ECOS) 접근과 인증키는 이 프로세스에만 둔다.

- market_snapshot: 금리·환율 조회 (MARKET_DATA_MODE=db 면 시장 DB 의 검사 통과 값만)
- collect_market_data: UC-02-1 수집·적재 (DATABASE_URL 이 있을 때만). 플랫폼 예약 실행이 부른다
리포트 작성·저장은 플랫폼이 한다.
"""

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


def build_server(service: MarketService, collector=None) -> MCPServer:
    """collector: 수집·적재 함수 (days → 결과). 있으면 collect_market_data 를 연다 (플랫폼 예약 실행만 부른다)."""
    server = MCPServer("market", version="0.1.0", instructions="ECOS 환율·기준금리 조회와 시장 DB 수집·적재. 보고서 작성·저장은 플랫폼이 한다. replay는 기록 재생이며 실시간이 아님.")

    if collector is not None:
        @server.tool()
        async def collect_market_data(days: Annotated[int, Field(ge=1, le=400)] = 10) -> dict:
            """UC-02-1 수집·적재: ECOS 에서 받아 받은 편지함에 놓고, 해시·형식 검사를 통과한 값만 시장 DB 에 넣는다."""
            try:
                return await collector(days)
            except MarketError as exc:
                raise ToolError(str(exc)) from exc

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


def build_http_app(service: MarketService, token: str | None = None, *, host: str = "127.0.0.1", collector=None):
    app = build_server(service, collector).streamable_http_app(host=host)
    if token:
        app.add_middleware(BearerTokenMiddleware, token=token)
    return app


def main():
    import uvicorn

    provider = provider_from_env()
    host, port = os.environ.get("MARKET_MCP_HOST", "127.0.0.1"), int(os.environ.get("MARKET_MCP_PORT", "8002"))
    service = MarketService(provider)  # 리포트 저장소 없음: 조회 결과·리포트는 플랫폼이 보관한다
    collector = None
    if os.environ.get("DATABASE_URL", "").strip():
        from .collect import collect_and_load, inbox_from_env, pg_store_from_env
        from .ecos import collect_provider_from_env

        store = provider.store if provider.mode == "db" else pg_store_from_env()

        async def collector(days: int) -> dict:
            return await collect_and_load(collect_provider_from_env(), store, inbox_from_env(), days=days)
    print(f"[market-mcp] mode={provider.mode} · 수집 {'가능' if collector else '꺼짐(DATABASE_URL 없음)'}"
          f" · http://{host}:{port}/mcp", flush=True)
    uvicorn.run(build_http_app(service, os.environ.get("MARKET_MCP_TOKEN"), host=host, collector=collector),
                host=host, port=port)


if __name__ == "__main__":
    main()
