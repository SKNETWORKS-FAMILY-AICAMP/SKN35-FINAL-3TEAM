"""Gmail MCP tools exposed through the platform harness and its approval policy."""

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Callable
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall

log = logging.getLogger(__name__)


class MailInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class SearchMailInput(MailInput):
    query: str = Field(default="", max_length=200, description="제목·보낸 사람·본문 검색어. 비우면 최근 메일")
    max_results: int = Field(default=10, ge=1, le=20, description="최대 검색 결과 수")


class ReadMailInput(MailInput):
    thread_id: str = Field(min_length=1, max_length=64, pattern=r"^[0-9a-zA-Z_-]+$", description="검색 결과의 메일 스레드 ID")


class SendMailInput(MailInput):
    to: list[str] = Field(min_length=1, max_length=10, description="받는 사람 이메일 목록")
    subject: str = Field(min_length=1, max_length=200, description="메일 제목")
    body: str = Field(min_length=1, max_length=20000, description="일반 텍스트 본문")
    cc: list[str] = Field(default_factory=list, max_length=10, description="참조 이메일 목록")


MAIL_TOOLS = {
    "search_mail": ("내 메일 검색", "연결된 본인 메일함을 검색합니다. 계정 미연결이면 연결 안내를 반환합니다.", SearchMailInput),
    "read_mail": ("메일 읽기", "검색에서 받은 thread_id로 본인 메일의 본문을 읽습니다.", ReadMailInput),
    "send_mail": ("메일 보내기", "본인 계정으로 메일을 보냅니다. 사용자가 발송을 요청한 경우에만 호출하며 하네스 승인 후 실행됩니다. simulated 결과는 가상 발송입니다.", SendMailInput),
}


class GmailMCPConnection:
    def __init__(self, url: str, *, token: str | None = None, user_map: dict[str, str] | None = None,
                 timeout: float = 25, client_factory: Callable[[], Client] | None = None) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("GMAIL_MCP_URL은 사용자 정보가 없는 HTTP(S) 주소여야 합니다.")
        self.url = url
        self._token = token
        self.user_map = dict(user_map or {})
        self.timeout = timeout
        self._client_factory = client_factory

    @asynccontextmanager
    async def session(self):
        if self._client_factory is not None:
            async with self._client_factory() as client:
                yield client
            return
        headers = {"Authorization": "Bearer " + self._token} if self._token else {}
        async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(self.timeout, connect=5)) as http:
            transport = streamable_http_client(self.url, http_client=http)
            async with Client(transport, read_timeout_seconds=self.timeout) as client:
                yield client


class GmailTool:
    def __init__(self, name: str, connection: GmailMCPConnection) -> None:
        display_name, description, model = MAIL_TOOLS[name]
        self.spec = ToolSpec(name=name, display_name=display_name, description=description,
                             effect="send" if name == "send_mail" else "read", domain="메일",
                             parameters=model.model_json_schema())
        self._model = model
        self.connection = connection

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            arguments = self._model.model_validate(call.arguments).model_dump()
        except ValidationError:
            return ToolResult(call.call_id, False, "메일 도구의 입력 형식과 길이를 확인해 주세요.")
        # requester_id is never accepted from the model, including on write calls.
        arguments["requester_id"] = self.connection.user_map.get(call.principal.user_id, call.principal.user_id)
        try:
            async with self.connection.session() as client:
                listing = await client.list_tools()
                if self.spec.name not in {tool.name for tool in listing.tools}:
                    return ToolResult(call.call_id, False, "Gmail MCP 서버에 해당 도구가 등록되어 있지 않습니다.")
                response = await client.call_tool(self.spec.name, arguments)
        except Exception as error:
            # Do not retry writes: a lost response can follow a successful send.
            log.warning("Gmail MCP call failed (%s): %s", self.spec.name, type(error).__name__)
            message = "Gmail MCP 서버에 연결하거나 응답을 읽지 못했습니다. 서버 상태와 계정 연결을 확인해 주세요."
            if self.spec.name == "send_mail":
                message += " 발송 여부가 불확실하므로 보낸편지함을 확인한 후 재시도해 주세요."
            return ToolResult(call.call_id, False, message)

        text = "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        payload = response.structured_content
        if response.is_error:
            return ToolResult(call.call_id, False, text or "Gmail MCP가 작업을 거부했습니다.")
        content = json.dumps(payload, ensure_ascii=False) if payload is not None else text
        if not content:
            return ToolResult(call.call_id, False, "Gmail MCP 결과가 비어 있습니다.")
        return ToolResult(call.call_id, True, content, data=payload)


def build_gmail_tools() -> list[GmailTool]:
    url = os.environ.get("GMAIL_MCP_URL", "http://127.0.0.1:8001/mcp").strip()
    if not url:
        return []
    mapping = json.loads(os.environ.get("GMAIL_MCP_USER_MAP", "{}"))
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str) and key and isinstance(value, str) and value
        for key, value in mapping.items()
    ):
        raise ValueError("GMAIL_MCP_USER_MAP은 사용자 ID를 사번에 연결하는 JSON 객체여야 합니다.")
    connection = GmailMCPConnection(url, token=os.environ.get("GMAIL_MCP_TOKEN"), user_map=mapping)
    return [GmailTool(name, connection) for name in MAIL_TOOLS]
