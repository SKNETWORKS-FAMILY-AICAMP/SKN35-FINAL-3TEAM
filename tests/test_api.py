"""FastAPI 앱 테스트. LLM 은 스크립트 응답(ScriptedLLM), 권한·감사·승인은 son 구현을 그대로 쓴다."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import Decision, Principal, PolicyRule
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from services.container import AppServices
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.arithmetic import arithmetic_tools
from tools.registry import SimpleToolRegistry

USERS = UserDirectory({
    "kim": Principal("kim", ("member",), {"department_id": 10, "security_level": 2}),
    "park": Principal("park", ("member",), {"department_id": 20}),
})
KIM = {"X-User-Id": "kim"}


class SendEmail:
    spec = ToolSpec("send_email", "메일 발송")

    async def run(self, call):
        return ToolResult(call.call_id, True, "sent")


def make(script, tmp_path, approval_timeout=5.0):
    rules = [
        PolicyRule("math", "add", Decision.ALLOW),
        PolicyRule("mail", "send_email", Decision.ASK, reason="메일이 발송됩니다"),
        PolicyRule("no-mul", "multiply", Decision.DENY, reason="곱셈 금지"),
    ]
    svc = AppServices(
        llm=ScriptedLLM(script),
        tools=SimpleToolRegistry([*arithmetic_tools(), SendEmail()]),
        checker=create_checker(rules=rules, default=Decision.DENY),
        approvals=FutureApprovalHandler(approval_timeout),
        audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=USERS,
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, svc


def tool_call(cid, name, **args):
    return {"tool_calls": [{"id": cid, "name": name, "arguments": args}]}


async def test_health(tmp_path):
    client, _ = make([], tmp_path)
    assert (await client.get("/health")).json() == {"status": "ok"}


async def test_requires_known_user(tmp_path):
    client, _ = make([], tmp_path)
    assert (await client.post("/api/chat/messages", json={"content": "hi"})).status_code == 401
    r = await client.post("/api/chat/messages", json={"content": "hi"}, headers={"X-User-Id": "ghost"})
    assert r.status_code == 401


async def test_chat_runs_tool_and_reports_events(tmp_path):
    client, svc = make([tool_call("c1", "add", a=3, b=4), {"content": "7입니다"}], tmp_path)
    r = await client.post("/api/chat/messages", json={"content": "3+4"}, headers=KIM)
    body = r.json()
    assert r.status_code == 200
    assert body["message"] == "7입니다" and body["stop_reason"] == "completed"
    assert body["tool_events"] == [
        {"call_id": "c1", "tool_name": "add", "display_name": "덧셈", "arguments": {"a": 3, "b": 4}, "result": "7", "ok": True}
    ]
    # 첫 메시지 앞에 시스템 프롬프트가 붙는다
    assert svc.llm.calls[0][0][0].role == "system"
    # 감사 로그에 run_id 와 요청자 속성이 남는다
    row = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert row["run_id"] == body["run_id"] and row["principal_snapshot"]["department_id"] == 10


async def test_denied_tool_stops_with_template(tmp_path):
    client, svc = make([tool_call("c1", "multiply", a=2, b=3)], tmp_path)
    body = (await client.post("/api/chat/messages", json={"content": "2*3"}, headers=KIM)).json()
    assert body["stop_reason"] == "denied"
    assert body["denial"] == {
        "call_id": "c1", "tool_name": "multiply", "display_name": "곱셈", "kind": "deny", "reason": "곱셈 금지",
    }
    assert "곱셈 금지" in body["message"] and "'곱셈' 작업은" in body["message"]
    assert body["tool_events"][0]["ok"] is False
    assert len(svc.llm.calls) == 1  # 성공한 툴이 없으니 LLM 을 다시 부르지 않는다


async def test_denied_after_success_gets_llm_wrapup(tmp_path):
    client, svc = make([
        {"tool_calls": [{"id": "c1", "name": "add", "arguments": {"a": 1, "b": 2}},
                        {"id": "c2", "name": "multiply", "arguments": {"a": 3, "b": 4}}]},
        {"content": "1+2=3 이에요. 곱셈은 권한이 없어 못 했어요."},
    ], tmp_path)
    body = (await client.post("/api/chat/messages", json={"content": "계산"}, headers=KIM)).json()
    assert body["stop_reason"] == "denied"
    assert body["message"] == "1+2=3 이에요. 곱셈은 권한이 없어 못 했어요."
    assert svc.llm.calls[-1][1] == []  # 마무리 호출은 툴 없이
    # 마무리 지시문은 대화 기록에 남지 않는다
    msgs = (await client.get(f"/api/chat/sessions/{body['session_id']}/messages", headers=KIM)).json()
    assert [m["role"] for m in msgs if m["role"] == "user"] == ["user"]


async def test_history_continues_and_is_private(tmp_path):
    client, svc = make([{"content": "첫 답"}, {"content": "둘째 답"}], tmp_path)
    first = (await client.post("/api/chat/messages", json={"content": "하나"}, headers=KIM)).json()
    sid = first["session_id"]
    await client.post("/api/chat/messages", json={"content": "둘", "session_id": sid}, headers=KIM)
    sent = [m.content for m in svc.llm.calls[1][0]]
    assert "하나" in sent and "첫 답" in sent

    msgs = (await client.get(f"/api/chat/sessions/{sid}/messages", headers=KIM)).json()
    assert [m["content"] for m in msgs] == ["하나", "첫 답", "둘", "둘째 답"]

    # 다른 사람은 이 세션을 읽거나 이어 쓸 수 없다
    park = {"X-User-Id": "park"}
    assert (await client.get(f"/api/chat/sessions/{sid}/messages", headers=park)).status_code == 404
    r = await client.post("/api/chat/messages", json={"content": "x", "session_id": sid}, headers=park)
    assert r.status_code == 404


@pytest.mark.parametrize("approved, expect", [(True, "sent"), (False, "not approved")])
async def test_approval_flow_through_api(tmp_path, approved, expect):
    client, _ = make([tool_call("m1", "send_email", to="a@b.c"), {"content": "끝"}], tmp_path)
    chat = asyncio.create_task(client.post("/api/chat/messages", json={"content": "메일 보내"}, headers=KIM))

    pending = []
    for _ in range(100):  # 채팅 요청이 승인 대기에 들어갈 때까지
        pending = (await client.get("/api/approvals", headers=KIM)).json()
        if pending:
            break
        await asyncio.sleep(0.01)
    assert pending and pending[0]["tool_name"] == "send_email" and pending[0]["reason"] == "메일이 발송됩니다"

    # 다른 사용자에게는 보이지도, 응답되지도 않는다
    park = {"X-User-Id": "park"}
    assert (await client.get("/api/approvals", headers=park)).json() == []
    assert (await client.post("/api/approvals/m1", json={"approved": True}, headers=park)).status_code == 404

    assert (await client.post("/api/approvals/m1", json={"approved": approved}, headers=KIM)).status_code == 204
    body = (await chat).json()
    assert expect in body["tool_events"][0]["result"]
    assert body["stop_reason"] == ("completed" if approved else "denied")
    if not approved:
        assert body["denial"]["kind"] == "rejected"
    rows = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert rows[0]["approval"] == ("approved" if approved else "rejected")


async def test_approval_timeout_is_reported_as_timeout(tmp_path):
    client, _ = make([tool_call("m1", "send_email", to="a@b.c")], tmp_path, approval_timeout=0.05)
    body = (await client.post("/api/chat/messages", json={"content": "메일 보내"}, headers=KIM)).json()
    assert body["stop_reason"] == "denied"
    assert body["denial"]["kind"] == "timeout"
    assert "응답이 없어" in body["message"]


async def test_display_names_in_response(tmp_path):
    client, _ = make([tool_call("a1", "add", a=1, b=2), tool_call("x1", "multiply", a=1, b=2)], tmp_path)
    body = (await client.post("/api/chat/messages", json={"content": "x"}, headers=KIM)).json()
    assert [e["display_name"] for e in body["tool_events"]] == ["덧셈", "곱셈"]
    assert body["denial"]["display_name"] == "곱셈"


async def test_llm_outage_returns_502(tmp_path):
    from openai import APIConnectionError

    class DownLLM:
        async def chat(self, messages, tools):
            raise APIConnectionError(request=httpx.Request("POST", "http://llm"))

    client, svc = make([], tmp_path)
    svc.llm = DownLLM()
    r = await client.post("/api/chat/messages", json={"content": "hi"}, headers=KIM)
    assert r.status_code == 502
