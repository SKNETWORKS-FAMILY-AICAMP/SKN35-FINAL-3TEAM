"""Platform-to-MCP bridge tests use only an in-memory fake mailbox."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from mcp import Client

pytest.importorskip("gmail_mcp")
from gmail_mcp.config import Settings
from gmail_mcp.fake import KIM_ID, CHOI_ID, KIM, LEE, sample_mailboxes
from gmail_mcp.service import MailService
from gmail_mcp.store import MailStore
from gmail_mcp.server import build_server, build_http_app

from contracts.permission import Decision, Principal, ToolCall
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import load_policy
from services.container import AppServices, build_from_env
from services.mail_accounts import GmailAccountLink
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.gmail import GmailMCPConnection, GmailTool, build_gmail_tools
from tools.registry import SimpleToolRegistry

ROOT = Path(__file__).resolve().parents[1]
USER = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})


@pytest.fixture
def bridge(tmp_path):
    boxes = sample_mailboxes()
    service = MailService(MailStore(tmp_path / "mail.sqlite3"), lambda owner: boxes[owner], Settings(adapter="fake"))
    server = build_server(service)
    connection = GmailMCPConnection(
        "http://127.0.0.1:8001/mcp", user_map={"kim": KIM_ID, "choi": CHOI_ID},
        client_factory=lambda: Client(server),
    )
    return connection, service, boxes


def call(name, arguments, user=USER):
    return ToolCall("mail-call", name, arguments, user, "session")


async def test_search_read_and_identity_injection(bridge):
    connection, _, _ = bridge
    search = GmailTool("search_mail", connection)
    assert "requester_id" not in search.spec.parameters["properties"]
    result = await search.run(call("search_mail", {}))
    assert result.ok
    payload = json.loads(result.content)
    assert payload["threads"]
    thread_id = payload["threads"][0]["thread_id"]
    read = await GmailTool("read_mail", connection).run(call("read_mail", {"thread_id": thread_id}))
    assert read.ok and json.loads(read.content)["messages"]
    denied = await search.run(call("search_mail", {"requester_id": CHOI_ID}))
    assert not denied.ok
    # A platform user cannot open another mailbox's exclusive thread by ID.
    other = await search.run(call("search_mail", {}, Principal("choi")))
    other_ids = {item["thread_id"] for item in json.loads(other.content)["threads"]}
    own_ids = {item["thread_id"] for item in payload["threads"]}
    private_id = next(iter(other_ids - own_ids))
    assert not (await GmailTool("read_mail", connection).run(call("read_mail", {"thread_id": private_id}))).ok


@pytest.mark.parametrize("approved", [True, False, None])
async def test_send_via_chat_uses_approval_and_fake_mail_only(bridge, tmp_path, approved):
    connection, service, boxes = bridge
    rules, default = load_policy(ROOT / "config/permissions.yaml")
    svc = AppServices(
        llm=ScriptedLLM([
            {"tool_calls": [{"id": "send-1", "name": "send_mail", "arguments": {
                "to": [LEE], "subject": "테스트", "body": "가상 발송 검증",
            }}]}, {"content": "결과 확인"},
        ]),
        tools=SimpleToolRegistry([GmailTool("send_mail", connection)]),
        checker=create_checker(rules=rules, default=default),
        approvals=FutureApprovalHandler(0.1 if approved is None else 3),
        audit=JsonlAuditLogger(tmp_path / "audit.jsonl"), users=UserDirectory({"kim": USER}),
    )
    admin = await svc.checker.check(call("send_mail", {}, Principal("admin", ("admin",))))
    assert admin.decision == Decision.ASK
    app = create_app(build=lambda: svc)
    app.state.services = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": "kim"}
        task = asyncio.create_task(client.post("/api/chat/messages", headers=headers, json={"content": "메일 보내줘"}))
        for _ in range(100):
            pending = (await client.get("/api/approvals", headers=headers)).json()
            if pending:
                break
            await asyncio.sleep(.005)
        assert pending and boxes[KIM_ID].sent == []
        if approved is not None:
            assert (await client.post("/api/approvals/send-1", headers=headers, json={"approved": approved})).status_code == 204
        response = (await task).json()
    assert len(boxes[KIM_ID].sent) == (1 if approved is True else 0)
    assert response["stop_reason"] == ("completed" if approved is True else "denied")
    if approved is True:
        assert json.loads(response["tool_events"][0]["result"])["status"] == "simulated"


async def test_http_token_required(bridge):
    _, service, _ = bridge
    from fastapi.testclient import TestClient
    with TestClient(build_http_app(service, token="test-token")) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/mcp", json={}, headers={"Authorization": "Bearer test-token"}).status_code != 401


def test_build_from_env_registers_all_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_MCP_URL", "http://127.0.0.1:8001/mcp")
    monkeypatch.setenv("GMAIL_MCP_USER_MAP", '{"kim": "E1001"}')
    monkeypatch.setenv("LLM_PROVIDER", "local_math")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("PARSE_TOOL_TIMEOUT_SECONDS", "123")
    monkeypatch.setenv("REVIEW_REQUEST_DB_PATH", str(tmp_path / "review.sqlite3"))
    async def resolve(user_id):
        return GmailAccountLink(user_id, "login-account")

    services = build_from_env(gmail_account_resolver=resolve)
    async def verify():
        specs = await services.tools.specs()
        assert {spec.name for spec in specs} >= {
            "parse_doc", "get_review_case", "submit_review_request", "today",
            "search_mail", "read_mail", "send_mail",
        }
        assert (await services.tools.get("parse_doc")).timeout == 123
        assert await (await services.tools.get("send_mail")).connection.requester_id_for(USER) == "login-account"
    asyncio.run(verify())


def test_gmail_tools_are_visible_with_default_endpoint(monkeypatch):
    monkeypatch.delenv("GMAIL_MCP_URL", raising=False)
    tools = build_gmail_tools()
    assert {tool.spec.name for tool in tools} == {"search_mail", "read_mail", "send_mail"}
    assert all(tool.connection.url == "http://127.0.0.1:8001/mcp" for tool in tools)


def test_gmail_disabled_with_explicit_empty_endpoint(monkeypatch):
    monkeypatch.setenv("GMAIL_MCP_URL", "")
    assert build_gmail_tools() == []


async def test_transport_failure_does_not_retry_send():
    attempts = []
    def failing_client():
        attempts.append(1)
        raise ConnectionError("test")
    connection = GmailMCPConnection("http://127.0.0.1:8001/mcp", client_factory=failing_client)
    result = await GmailTool("send_mail", connection).run(call("send_mail", {"to": [LEE], "subject": "테스트", "body": "본문"}))
    assert not result.ok and len(attempts) == 1 and "보낸편지함" in result.content


async def test_login_account_resolver_sends_from_each_users_mailbox(bridge):
    _, service, boxes = bridge
    looked_up = []

    async def resolve(user_id):
        looked_up.append(user_id)
        return GmailAccountLink(user_id, {"kim": KIM_ID, "choi": CHOI_ID}[user_id])

    connection = GmailMCPConnection(
        "http://127.0.0.1:8001/mcp", account_resolver=resolve,
        # Resolver wins over the old environment mapping.
        user_map={"kim": CHOI_ID}, client_factory=lambda: Client(build_server(service)),
    )
    tool = GmailTool("send_mail", connection)
    for user in (USER, Principal("choi")):
        result = await tool.run(call("send_mail", {"to": [LEE], "subject": "계정별 검증", "body": "가상 발송"}, user))
        assert result.ok and result.data["status"] == "simulated"
    assert looked_up == ["kim", "choi"]
    assert len(boxes[KIM_ID].sent) == len(boxes[CHOI_ID].sent) == 1


@pytest.mark.parametrize("name,args", [
    ("search_mail", {}), ("read_mail", {"thread_id": "kt1"}),
    ("send_mail", {"to": [LEE], "subject": "제목", "body": "본문"}),
])
async def test_unlinked_login_account_never_calls_mcp_or_falls_back(name, args):
    async def resolve(user_id):
        assert user_id == "kim"
        return None

    def unexpected_client():
        pytest.fail("Unlinked accounts must not reach MCP")

    connection = GmailMCPConnection("http://127.0.0.1:8001/mcp", account_resolver=resolve,
                                    user_map={"kim": KIM_ID}, client_factory=unexpected_client)
    result = await GmailTool(name, connection).run(call(name, args))
    assert not result.ok and "연결되지 않았습니다" in result.content


@pytest.mark.parametrize("link", [GmailAccountLink("choi", CHOI_ID), GmailAccountLink("kim", "../other"),
                                      GmailAccountLink("kim", "someone@gmail.com")])
async def test_invalid_login_account_link_is_not_used(link):
    async def resolve(user_id):
        return link

    def unexpected_client():
        pytest.fail("Invalid account links must not reach MCP")

    connection = GmailMCPConnection("http://127.0.0.1:8001/mcp", account_resolver=resolve,
                                    client_factory=unexpected_client)
    result = await GmailTool("search_mail", connection).run(call("search_mail", {}))
    assert not result.ok and "확인할 수 없습니다" in result.content


@pytest.mark.parametrize("field", ["from", "sender", "requester_id", "mailbox_user_id"])
async def test_model_cannot_choose_sender(field):
    def unexpected_client():
        pytest.fail("Spoofed sender input must not reach MCP")

    connection = GmailMCPConnection("http://127.0.0.1:8001/mcp", client_factory=unexpected_client)
    result = await GmailTool("send_mail", connection).run(call("send_mail", {
        "to": [LEE], "subject": "제목", "body": "본문", field: CHOI_ID,
    }))
    assert not result.ok and "입력 형식" in result.content


async def test_account_link_is_looked_up_again_after_disconnect(bridge):
    _, service, _ = bridge
    links = {"kim": GmailAccountLink("kim", KIM_ID)}

    async def resolve(user_id):
        return links.get(user_id)

    connection = GmailMCPConnection("http://127.0.0.1:8001/mcp", account_resolver=resolve,
                                    client_factory=lambda: Client(build_server(service)))
    tool = GmailTool("search_mail", connection)
    assert (await tool.run(call("search_mail", {}))).ok
    del links["kim"]
    result = await tool.run(call("search_mail", {}))
    assert not result.ok and "연결되지 않았습니다" in result.content


async def test_login_account_lookup_failure_does_not_send():
    async def resolve(user_id):
        raise RuntimeError("account service unavailable")

    def unexpected_client():
        pytest.fail("Account lookup failures must not reach MCP")

    connection = GmailMCPConnection("http://127.0.0.1:8001/mcp", account_resolver=resolve,
                                    client_factory=unexpected_client)
    result = await GmailTool("send_mail", connection).run(call("send_mail", {
        "to": [LEE], "subject": "제목", "body": "본문",
    }))
    assert not result.ok and "연결 정보를 확인할 수 없습니다" in result.content


async def picker_app(bridge, tmp_path, decision=Decision.ASK):
    from gmail_mcp.fake import CHOI
    from services.mail_dispatches import MailDispatchStore
    connection, service, boxes = bridge
    await service.store.set_account(KIM_ID, KIM)
    await service.store.set_account(CHOI_ID, CHOI)
    svc = AppServices(
        llm=ScriptedLLM([]), tools=SimpleToolRegistry([GmailTool("send_mail", connection)]),
        checker=create_checker(rules=[], default=decision), approvals=FutureApprovalHandler(2),
        audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory({"kim": USER, "choi": Principal("choi", attributes={"name": "동료"}), "unconnected": Principal("unconnected")}),
        mail_dispatches=MailDispatchStore(tmp_path / "dispatch.sqlite3"),
    )
    session = await svc.sessions.create("kim")
    app = create_app(build=lambda: svc)
    app.state.services = svc
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-User-Id": "kim"}), svc, session, boxes


def draft_request(session):
    import uuid
    return {"session_id": session.session_id, "request_id": str(uuid.uuid4()),
            "recipient_ids": ["choi"], "subject": "업무 안내", "body": "초안입니다."}


async def test_picker_lists_only_other_connected_staff(bridge, tmp_path):
    client, _, session, boxes = await picker_app(bridge, tmp_path)
    async with client:
        response = await client.get("/api/mail/recipients", params={"session_id": session.session_id})
        assert response.status_code == 200
        assert [item["user_id"] for item in response.json()] == ["choi"]
        assert response.json()[0]["name"] == "동료"
    assert boxes[KIM_ID].sent == []


@pytest.mark.parametrize("approve", [True, False])
async def test_picker_approval_and_idempotent_replay(bridge, tmp_path, approve):
    client, svc, session, boxes = await picker_app(bridge, tmp_path)
    body = draft_request(session)
    async with client:
        task = asyncio.create_task(client.post("/api/mail/send", json=body))
        for _ in range(100):
            pending = (await client.get("/api/approvals")).json()
            if pending:
                break
            await asyncio.sleep(.005)
        assert pending and boxes[KIM_ID].sent == []
        assert pending[0]["arguments"]["body"] == body["body"]
        assert (await client.post("/api/mail/send", json=body)).status_code == 409
        # 다른 직원은 발신자의 승인을 처리하지 못한다.
        assert (await client.post("/api/approvals/" + body["request_id"], headers={"X-User-Id": "choi"}, json={"approved": True})).status_code == 404
        await client.post("/api/approvals/" + body["request_id"], json={"approved": approve})
        response = await task
        assert response.status_code == 200
        assert response.json()["status"] == ("simulated" if approve else "not_sent")
        assert (await client.post("/api/mail/send", json=body)).json() == response.json()
        assert (await client.post("/api/mail/send", json={**body, "subject": "다른 제목"})).status_code == 409
    assert len(boxes[KIM_ID].sent) == int(approve)
    assert session.messages[-1].content == response.json()["message"]
    assert len(svc.llm.calls) == 0


@pytest.mark.parametrize("uid", ["kim", "unconnected", "unknown"])
async def test_picker_rejects_self_and_unconnected(bridge, tmp_path, uid):
    client, _, session, boxes = await picker_app(bridge, tmp_path)
    async with client:
        body = {**draft_request(session), "recipient_ids": [uid]}
        assert (await client.post("/api/mail/send", json=body)).status_code == 422
    assert not boxes[KIM_ID].sent


async def test_picker_session_ownership_and_tool_selection(bridge, tmp_path):
    from services.agents import Agent
    client, svc, session, boxes = await picker_app(bridge, tmp_path)
    async with client:
        body = draft_request(session)
        assert (await client.post("/api/mail/send", headers={"X-User-Id": "choi"}, json=body)).status_code == 404
        agent = Agent("without-mail", "kim", 10, "문서 도우미", "", ())
        # 저장소 메서드와 무관하게 이 테스트에서 사용할 에이전트를 둔다.
        svc.agents._agents[agent.agent_id] = agent
        session.agent_id = agent.agent_id
        assert (await client.post("/api/mail/send", json=body)).status_code == 409
    assert not boxes[KIM_ID].sent


@pytest.mark.parametrize("decision", [Decision.DENY, Decision.ASK])
async def test_picker_denial_or_timeout_never_sends(bridge, tmp_path, decision):
    client, _, session, boxes = await picker_app(bridge, tmp_path, decision)
    async with client:
        response = await client.post("/api/mail/send", json=draft_request(session))
        assert response.json()["status"] == "not_sent"
    assert not boxes[KIM_ID].sent


async def test_picker_uncertain_send_is_never_retried_even_with_reopened_store(bridge, tmp_path):
    from contracts.harness import ToolResult
    from services.mail_dispatches import MailDispatchStore
    client, svc, session, _ = await picker_app(bridge, tmp_path, Decision.ALLOW)
    calls = []

    async def uncertain(call):
        calls.append(call)
        return ToolResult(call.call_id, False, "응답 유실")

    (await svc.tools.get("send_mail")).run = uncertain
    body = draft_request(session)
    async with client:
        response = await client.post("/api/mail/send", json=body)
        assert response.json()["status"] == "unknown"
        svc.mail_dispatches = MailDispatchStore(tmp_path / "dispatch.sqlite3")
        assert (await client.post("/api/mail/send", json=body)).json() == response.json()
    assert len(calls) == 1


async def test_google_send_does_not_retry_server_error(monkeypatch):
    from unittest.mock import MagicMock
    from googleapiclient.errors import HttpError
    from httplib2 import Response
    from gmail_mcp.client import GoogleGmailClient
    client = GoogleGmailClient(Settings(adapter="google"), KIM_ID, max_retries=3)
    service = MagicMock()
    execute = service.users.return_value.messages.return_value.send.return_value.execute
    execute.side_effect = HttpError(Response({"status": "503"}), b"unavailable")
    monkeypatch.setattr(client, "_service", lambda: service)
    with pytest.raises(HttpError):
        await client.send_message("fake-raw")
    execute.assert_called_once_with(num_retries=0)
