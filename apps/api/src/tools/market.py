"""시장 MCP를 기존 에이전트 ToolRegistry·권한·감사 경로에 연결한다."""

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall

log = logging.getLogger(__name__)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class SnapshotInput(Input):
    base_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD. 생략하면 한국 시간 오늘")


class SaveInput(Input):
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="market_snapshot이 반환한 본인 조회 결과 ID")
    summary: str = Field(default="", max_length=2000, description="조회 결과로 작성한 3문장 이내 요약. 비우면 표만 저장")
    template_selection_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$", description="get_report_template이 반환한 본인 양식 선택 ID. 생략하면 기본 양식")


class SearchTemplateInput(Input):
    query: str = Field(default="", max_length=2000, description="업무 목적이나 양식 이름. 비우면 양식 목록")


class GetTemplateInput(Input):
    template_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$", description="양식 검색 결과의 template_id")
    expected_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="검색 결과의 template_hash. 검색 후 변경 여부 검사")


class GetInput(Input):
    report_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="저장 결과의 리포트 번호")


MARKET_TOOLS = {
    "search_report_templates": ("보고서 양식 검색", "업무 목적에 맞는 보고서 양식을 검색합니다. 검색 후보만으로 작성하지 말고 선택한 양식을 get_report_template으로 읽으세요.", SearchTemplateInput, "read"),
    "get_report_template": ("보고서 양식 전체 읽기", "선택한 양식의 전체 항목·필수 입력·버전을 읽습니다. template_selection_id를 저장 도구에 전달하면 읽은 버전으로 작성합니다.", GetTemplateInput, "read"),
    "market_snapshot": ("금리·환율 조회", "별도 MCP 서버가 한국은행 ECOS에서 USD·JPY(100엔)·EUR·CNY와 기준금리를 조회합니다. 실제 기준일·변동률·출처를 반환하며 replay는 과거 기록 재생입니다.", SnapshotInput, "read"),
    "save_morning_report": ("아침 리포트 저장", "본인 snapshot_id·template_selection_id와 요약으로 선택한 양식의 보고서를 저장합니다. 표와 필수 항목은 MCP가 채우며 요약을 검사합니다.", SaveInput, "write"),
    "get_morning_report": ("저장한 리포트 읽기", "본인이 저장한 리포트 번호로 원문을 다시 읽습니다.", GetInput, "read"),
}


class MarketMCPConnection:
    def __init__(self, url: str, *, token: str | None = None, client_factory=None, timeout: float = 25):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("MARKET_MCP_URL은 사용자 정보가 없는 HTTP(S) 주소여야 합니다.")
        self.url, self.token = url, token
        self.client_factory, self.timeout = client_factory, timeout

    @asynccontextmanager
    async def session(self):
        if self.client_factory:
            async with self.client_factory() as client:
                yield client
            return
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(self.timeout, connect=5)) as http:
            async with Client(streamable_http_client(self.url, http_client=http), read_timeout_seconds=self.timeout) as client:
                yield client


class MarketTool:
    def __init__(self, name: str, connection: MarketMCPConnection):
        label, description, model, effect = MARKET_TOOLS[name]
        self.spec = ToolSpec(name, description, model.model_json_schema(), label, effect, "시장")
        self.model, self.connection = model, connection

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            args = self.model.model_validate(call.arguments).model_dump(exclude_none=True)
        except ValidationError:
            return ToolResult(call.call_id, False, "시장 도구의 날짜·조회 결과 ID·요약 입력을 확인해 주세요.")
        args["requester_id"] = call.principal.user_id
        try:
            async with self.connection.session() as client:
                result = await client.call_tool(self.spec.name, args)
            if result.is_error:
                text = "\n".join(b.text for b in result.content if getattr(b, "type", "") == "text")
                return ToolResult(call.call_id, False, text or "시장 MCP가 요청을 거부했습니다.")
            payload = result.structured_content
            if payload is None:
                text = "\n".join(b.text for b in result.content if getattr(b, "type", "") == "text")
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
            if not isinstance(payload, dict) or not isinstance(payload.get("text", payload.get("markdown")), str):
                return ToolResult(call.call_id, False, "시장 MCP의 응답 형식을 확인할 수 없습니다.")
            return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
        except Exception as exc:
            log.warning("시장 MCP 호출 실패 (%s): %s", self.spec.name, type(exc).__name__)
            message = "시장 MCP 서버에 연결하거나 응답을 읽지 못했습니다. 서버 실행 상태를 확인해 주세요."
            if self.spec.name == "save_morning_report":
                message += " 저장 여부가 불확실하므로 결과를 확인한 후 다시 요청해 주세요. 자동 재시도하지 않았습니다."
            return ToolResult(call.call_id, False, message)


def build_market_tools() -> list[MarketTool]:
    url = os.environ.get("MARKET_MCP_URL", "http://127.0.0.1:8002/mcp").strip()
    if not url:
        return []
    connection = MarketMCPConnection(url, token=os.environ.get("MARKET_MCP_TOKEN"))
    return [MarketTool(name, connection) for name in MARKET_TOOLS]
