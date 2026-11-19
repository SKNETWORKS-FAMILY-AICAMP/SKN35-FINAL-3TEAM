"""커스텀 에이전트: 조회·관리 권한 표, API, 채팅 연결."""

from __future__ import annotations

import httpx
import pytest

from contracts.permission import Decision, PolicyRule, Principal
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from services import agents as rules
from services.agents import Agent
from services.container import AppServices
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.arithmetic import arithmetic_tools
from tools.registry import SimpleToolRegistry

KIM = Principal("kim", ("member",), {"department_id": 10})  # 만든 사람
CHOI = Principal("choi", ("member",), {"department_id": 10})  # 같은 부서 동료
LEE = Principal("lee", ("dept_head",), {"department_id": 10})  # kim 의 부서장
HAN = Principal("han", ("dept_head",), {"department_id": 20})  # 다른 부서 부서장
PARK = Principal("park", ("member",), {"department_id": 20})  # 다른 부서
ADMIN = Principal("admin", ("admin",), {"department_id": 1})
USERS = {p.user_id: p for p in (KIM, CHOI, LEE, HAN, PARK, ADMIN)}


def agent(scope="personal", status="active", disabled_by=None) -> Agent:
    return Agent("a1", "kim", 10, "계산", "p", scope=scope, status=status, disabled_by=disabled_by)


# ── 권한 표 ─────────────────────────────────────────────────

@pytest.mark.parametrize("who, scope, view, edit, disable", [
    (KIM, "personal", True, True, True),     # 주인: 다 함
    (CHOI, "personal", False, False, False),  # 동료: 개인 에이전트는 못 봄
    (CHOI, "dept", True, False, False),       # 부서 공유면 봄, 관리는 못 함
    (PARK, "dept", False, False, False),      # 다른 부서는 부서 공유를 못 봄
    (PARK, "company", True, False, False),    # 전사 공유는 다 봄
    (LEE, "personal", True, False, True),     # 부서장: 부서원 것은 보고 비활성화만
    (HAN, "personal", False, False, False),   # 다른 부서 부서장은 못 봄
    (ADMIN, "personal", True, True, True),    # admin: 전부
])
def test_permission_table(who, scope, view, edit, disable):
    a = agent(scope)
    assert (rules.can_view(who, a), rules.can_edit(who, a), rules.can_disable(who, a)) == (view, edit, disable)


def test_disabled_agent_is_hidden_from_shared_users_and_unusable():
    a = agent("company", status="disabled", disabled_by="lee")
    assert not rules.can_view(PARK, a)
    assert rules.can_view(KIM, a) and not rules.can_use(KIM, a)


def test_owner_cannot_reenable_what_dept_head_disabled():
    assert not rules.can_enable(KIM, agent(status="disabled", disabled_by="lee"))
    assert rules.can_enable(KIM, agent(status="disabled", disabled_by="kim"))
    assert rules.can_enable(LEE, agent(status="disabled", disabled_by="lee"))


# ── API ────────────────────────────────────────────────────

def h(p: Principal) -> dict[str, str]:
    return {"X-User-Id": p.user_id}


def make(script=(), tmp_path=None):
    svc = AppServices(
        llm=ScriptedLLM(list(script)),
        tools=SimpleToolRegistry(arithmetic_tools()),
        checker=create_checker(rules=[PolicyRule("all", "*", Decision.ALLOW)], default=Decision.DENY),
        approvals=FutureApprovalHandler(5),
        audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory(USERS),
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), svc


BODY = {"name": "계산 도우미", "system_prompt": "계산은 툴로 한다.", "tools": ["add"]}


async def test_create_and_list(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    r = await client.post("/api/agents", json=BODY, headers=h(KIM))
    assert r.status_code == 201
    a = r.json()
    assert (a["owner_user_id"], a["scope"], a["status"], a["tools"]) == ("kim", "personal", "active", ["add"])

    assert [x["agent_id"] for x in (await client.get("/api/agents?mine=true", headers=h(KIM))).json()] == [a["agent_id"]]
    assert (await client.get("/api/agents", headers=h(CHOI))).json() == []
    lee_view = (await client.get("/api/agents", headers=h(LEE))).json()
    assert [(x["can_edit"], x["can_disable"]) for x in lee_view] == [(False, True)]
    assert len((await client.get("/api/agents", headers=h(ADMIN))).json()) == 1


async def test_tools_must_be_registered(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    r = await client.post("/api/agents", json={**BODY, "tools": ["add", "rm_rf"]}, headers=h(KIM))
    assert r.status_code == 422 and "rm_rf" in r.json()["detail"]
    r = await client.post("/api/agents", json={**BODY, "tools": ["add", "add"]}, headers=h(KIM))
    assert r.status_code == 422
    tools = (await client.get("/api/tools", headers=h(KIM))).json()
    assert {t["name"]: t["display_name"] for t in tools}["add"] == "덧셈"


async def test_update_rules(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    aid = (await client.post("/api/agents", json=BODY, headers=h(KIM))).json()["agent_id"]
    url = f"/api/agents/{aid}"

    r = await client.patch(url, json={"name": "새 이름", "tools": ["add", "multiply"]}, headers=h(KIM))
    assert r.status_code == 200 and r.json()["name"] == "새 이름" and r.json()["tools"] == ["add", "multiply"]
    assert (await client.patch(url, json={"scope": "company"}, headers=h(KIM))).json()["scope"] == "personal"
    assert (await client.patch(url, json={"name": "x"}, headers=h(LEE))).status_code == 403  # 부서장은 수정 불가
    assert (await client.patch(url, json={"name": "x"}, headers=h(PARK))).status_code == 404  # 안 보이면 404
    assert (await client.patch(url, json={"name": "y"}, headers=h(ADMIN))).status_code == 200
    assert (await client.delete(url, headers=h(LEE))).status_code == 403
    assert (await client.delete(url, headers=h(KIM))).status_code == 204
    assert (await client.get(url, headers=h(KIM))).status_code == 404


async def test_dept_head_disables_and_owner_cannot_reenable(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    aid = (await client.post("/api/agents", json=BODY, headers=h(KIM))).json()["agent_id"]
    assert (await client.post(f"/api/agents/{aid}/disable", headers=h(HAN))).status_code == 404
    r = await client.post(f"/api/agents/{aid}/disable", headers=h(LEE))
    assert r.json()["status"] == "disabled"
    assert (await client.post(f"/api/agents/{aid}/enable", headers=h(KIM))).status_code == 403
    assert (await client.post(f"/api/agents/{aid}/enable", headers=h(LEE))).json()["status"] == "active"


# ── 채팅 연결 ───────────────────────────────────────────────

def call(cid, name, **args):
    return {"tool_calls": [{"id": cid, "name": name, "arguments": args}]}


async def test_chat_uses_agent_prompt_and_only_its_tools(tmp_path):
    client, svc = make([call("c1", "add", a=1, b=2), {"content": "3"}, {"content": "끝"}], tmp_path)
    aid = (await client.post("/api/agents", json=BODY, headers=h(KIM))).json()["agent_id"]

    r = await client.post("/api/chat/messages", json={"content": "1+2", "agent_id": aid}, headers=h(KIM))
    assert r.status_code == 200 and r.json()["message"] == "3"
    messages, tools = svc.llm.calls[0]
    assert [t.name for t in tools] == ["add"]
    assert messages[0].role == "system" and "계산은 툴로 한다." in messages[0].content
    rows = (tmp_path / "audit.jsonl").read_text()
    assert f'"agent_id": "{aid}"' in rows

    # 프롬프트를 고치면 이어지는 대화에도 바로 반영된다
    await client.patch(f"/api/agents/{aid}", json={"system_prompt": "바뀐 역할"}, headers=h(KIM))
    sid = r.json()["session_id"]
    await client.post("/api/chat/messages", json={"content": "또", "session_id": sid}, headers=h(KIM))
    sent = svc.llm.calls[-1][0]
    assert "바뀐 역할" in sent[0].content and [m.role for m in sent].count("system") == 1


async def test_chat_rejects_hidden_or_disabled_agent(tmp_path):
    client, _ = make([{"content": "ok"}], tmp_path)
    aid = (await client.post("/api/agents", json=BODY, headers=h(KIM))).json()["agent_id"]
    r = await client.post("/api/chat/messages", json={"content": "x", "agent_id": aid}, headers=h(PARK))
    assert r.status_code == 404

    sid = (await client.post("/api/chat/messages", json={"content": "x", "agent_id": aid}, headers=h(KIM))).json()["session_id"]
    await client.post(f"/api/agents/{aid}/disable", headers=h(LEE))
    r = await client.post("/api/chat/messages", json={"content": "y", "session_id": sid}, headers=h(KIM))
    assert r.status_code == 409
