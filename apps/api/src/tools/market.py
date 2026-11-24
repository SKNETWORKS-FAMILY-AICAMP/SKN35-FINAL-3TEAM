"""금리·환율 리포트(UC-02) 도구. 기존 에이전트 ToolRegistry·권한·감사 경로를 그대로 지난다.

- market_snapshot: 시장 MCP(별도 프로세스, ECOS 인증키 보관)가 조회만 한다. 받은 결과는 플랫폼이 보관한다
  (리포트 저장 때 표를 이 원본으로 다시 만들기 위해. 모델이 넘긴 수치는 쓰지 않는다).
- 양식 검색·양식 읽기·리포트 저장·리포트 읽기: 플랫폼 안에서 시장 MCP 패키지의 작성 코드(MarketService)를 라이브러리로
  쓰고, 저장은 API 문서 저장소(services/documents.py)에 한다. 저장한 리포트는 다른 서류와 같은 내려받기(PDF·DOCX)를 쓴다.
도구 이름·입력·결과 형식은 이전(모두 MCP)과 같아서 하네스(harness/market_grounding.py, runner._run_market)는 그대로다.
"""

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall
from market_mcp.ecos import MarketError
from market_mcp.service import MarketService

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
    "save_morning_report": ("아침 리포트 저장", "본인 snapshot_id·template_selection_id와 요약으로 선택한 양식의 보고서를 저장합니다. 표와 필수 항목은 플랫폼이 보관한 조회 결과로 채우며 요약을 검사합니다.", SaveInput, "write"),
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
    """시장 MCP 를 부르는 도구 (market_snapshot). recorder 가 있으면 성공한 조회 결과를 플랫폼에 보관한다."""

    def __init__(self, name: str, connection: MarketMCPConnection, recorder=None):
        label, description, model, effect = MARKET_TOOLS[name]
        self.spec = ToolSpec(name, description, model.model_json_schema(), label, effect, "시장")
        self.model, self.connection, self.recorder = model, connection, recorder

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
            if self.recorder is not None and self.spec.name == "market_snapshot":
                # 소유자는 로그인 신원으로 붙인다. 이후 저장은 이 보관본만 읽는다.
                snapshot = {k: v for k, v in payload.items() if k not in ("snapshot_id", "markdown")}
                await asyncio.to_thread(self.recorder.write, "snapshots",
                                        {**snapshot, "id": payload["snapshot_id"], "owner_user_id": call.principal.user_id})
            return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
        except Exception as exc:
            log.warning("시장 MCP 호출 실패 (%s): %s", self.spec.name, type(exc).__name__)
            message = "시장 MCP 서버에 연결하거나 응답을 읽지 못했습니다. 서버 실행 상태를 확인해 주세요."
            if self.spec.name == "save_morning_report":
                message += " 저장 여부가 불확실하므로 결과를 확인한 후 다시 요청해 주세요. 자동 재시도하지 않았습니다."
            return ToolResult(call.call_id, False, message)


LOCAL_TOOLS = ("search_report_templates", "get_report_template", "save_morning_report", "get_morning_report")


class LocalReportTool:
    """플랫폼 안에서 실행하는 리포트 도구. 작성 규칙은 시장 MCP 패키지 코드(MarketService)를 그대로 쓴다."""

    def __init__(self, name: str, reports: MarketService):
        label, description, model, effect = MARKET_TOOLS[name]
        self.spec = ToolSpec(name, description, model.model_json_schema(), label, effect, "시장")
        self.model, self.reports = model, reports

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            args = self.model.model_validate(call.arguments).model_dump(exclude_none=True)
        except ValidationError:
            return ToolResult(call.call_id, False, "리포트 도구의 양식·조회 결과 ID·요약 입력을 확인해 주세요.")
        try:
            payload = await getattr(self.reports, self.spec.name)(call.principal.user_id, **args)
        except MarketError as exc:
            return ToolResult(call.call_id, False, str(exc))
        except (OSError, ValueError, sqlite3.Error):
            log.warning("리포트 도구 실패 (%s)", self.spec.name, exc_info=True)
            return ToolResult(call.call_id, False, "리포트 자료를 저장하거나 읽지 못했습니다.")
        if "report_id" in payload and payload.get("document"):
            # 화면은 document_id 가 있으면 다른 서류와 같은 PDF·DOCX 버튼을 붙인다
            payload = {**payload, "document_id": payload["report_id"], "document_title": payload["document"]["title"]}
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)


class MarketRecords:
    """MarketService 의 저장소를 API 문서 저장소로 잇는다. 리포트는 문서로, 조회 결과·양식 선택은 작업 기록으로."""

    def __init__(self, documents) -> None:
        self.documents = documents

    def write(self, kind: str, payload: dict) -> None:
        if kind == "reports":
            self.documents.put({**payload, "document_id": payload["id"], "owner_id": payload["owner_user_id"],
                                "kind": "market_report", "title": payload["template"]["name"]})
        else:
            self.documents.put_record(kind, payload["id"], payload["owner_user_id"], payload)

    def read(self, kind: str, ident: str, owner: str) -> dict:
        found = (self.documents.get(owner, ident) if kind == "reports"
                 else self.documents.get_record(kind, ident, owner))
        if not found or (kind == "reports" and found.get("kind") != "market_report"):
            raise MarketError("열람 가능한 시장 자료를 찾을 수 없습니다.")
        return found


def report_service(documents, templates_dir: Path | None = None) -> MarketService:
    return MarketService(None, store=MarketRecords(documents), templates_dir=templates_dir)


def build_market_tools(reports: MarketService | None = None) -> list:
    """MARKET_MCP_URL 을 비우면 시장 도구를 등록하지 않는다. reports 가 없으면 조회 도구만."""
    url = os.environ.get("MARKET_MCP_URL", "http://127.0.0.1:8002/mcp").strip()
    if not url:
        return []
    connection = MarketMCPConnection(url, token=os.environ.get("MARKET_MCP_TOKEN"))
    recorder = reports.store if reports is not None else None
    tools: list = [MarketTool("market_snapshot", connection, recorder)]
    if reports is not None:
        tools += [LocalReportTool(name, reports) for name in LOCAL_TOOLS]
    return tools
