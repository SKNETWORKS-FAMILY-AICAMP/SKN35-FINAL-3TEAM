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

KIM = Principal("kim", ("member",), {"dept_id": 10})  # 만든 사람
CHOI = Principal("choi", ("member",), {"dept_id": 10})  # 같은 부서 동료
LEE = Principal("lee", ("dept_head",), {"dept_id": 10})  # kim 의 부서장
HAN = Principal("han", ("dept_head",), {"dept_id": 20})  # 다른 부서 부서장
PARK = Principal("park", ("member",), {"dept_id": 20})  # 다른 부서
ADMIN = Principal("admin", ("admin",), {"dept_id": 1})
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


# ── 공유 요청 ───────────────────────────────────────────────

def test_superior_is_dept_head_then_admin():
    users = UserDirectory(USERS)
    assert users.superior_of(KIM).user_id == "lee"
    assert users.superior_of(PARK).user_id == "han"
    assert users.superior_of(LEE).user_id == "admin"
    assert users.superior_of(ADMIN) is None
    lonely = Principal("solo", ("member",), {"dept_id": 99})  # 부서장 없는 부서
    assert users.superior_of(lonely).user_id == "admin"


async def _new_agent(client, who=KIM):
    return (await client.post("/api/agents", json=BODY, headers=h(who))).json()["agent_id"]


async def test_share_to_dept_needs_dept_head_approval(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    aid = await _new_agent(client)

    r = await client.post(f"/api/agents/{aid}/share-requests", json={"scope": "dept", "comment": "팀 공용"}, headers=h(KIM))
    assert r.status_code == 201
    req = r.json()
    assert (req["approver_id"], req["status"], req["can_decide"]) == ("lee", "pending", False)

    # 승인 전에는 동료에게 안 보인다, 중복 요청은 막힌다
    assert (await client.get("/api/agents", headers=h(CHOI))).json() == []
    again = await client.post(f"/api/agents/{aid}/share-requests", json={"scope": "company"}, headers=h(KIM))
    assert again.status_code == 409
    agent = (await client.get(f"/api/agents/{aid}", headers=h(KIM))).json()
    assert agent["pending_share_scope"] == "dept" and agent["can_request_share"] is False

    # 받은 사람만 inbox 에 보이고 결정할 수 있다
    rid = req["request_id"]
    assert [x["request_id"] for x in (await client.get("/api/share-requests?box=inbox", headers=h(LEE))).json()] == [rid]
    assert (await client.get("/api/share-requests?box=inbox", headers=h(KIM))).json() == []
    assert (await client.post(f"/api/share-requests/{rid}/approve", headers=h(CHOI))).status_code == 404
    assert (await client.post(f"/api/share-requests/{rid}/approve", headers=h(HAN))).status_code == 404
    assert (await client.post(f"/api/share-requests/{rid}/approve", headers=h(KIM))).status_code == 403  # 셀프 승인 불가

    done = await client.post(f"/api/share-requests/{rid}/approve", json={"comment": "좋아요"}, headers=h(LEE))
    assert done.json()["status"] == "approved" and done.json()["decided_by"] == "lee"
    assert (await client.post(f"/api/share-requests/{rid}/approve", headers=h(LEE))).status_code == 409

    # 같은 부서 동료는 보고 쓸 수 있고, 다른 부서는 못 본다
    assert [a["scope"] for a in (await client.get("/api/agents", headers=h(CHOI))).json()] == ["dept"]
    assert (await client.get("/api/agents", headers=h(PARK))).json() == []
    assert [x["status"] for x in (await client.get("/api/share-requests?box=outbox", headers=h(KIM))).json()] == ["approved"]


async def test_reject_keeps_personal_and_admin_can_decide(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    aid = await _new_agent(client)
    rid = (await client.post(f"/api/agents/{aid}/share-requests", json={"scope": "company"}, headers=h(KIM))).json()["request_id"]
    r = await client.post(f"/api/share-requests/{rid}/reject", json={"comment": "전사는 과함"}, headers=h(ADMIN))
    assert r.json()["status"] == "rejected" and r.json()["decision_comment"] == "전사는 과함"
    assert (await client.get(f"/api/agents/{aid}", headers=h(KIM))).json()["scope"] == "personal"


async def test_dept_head_request_goes_to_admin_and_admin_applies_directly(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    lee_agent = await _new_agent(client, LEE)
    r = (await client.post(f"/api/agents/{lee_agent}/share-requests", json={"scope": "company"}, headers=h(LEE))).json()
    assert r["approver_id"] == "admin" and r["status"] == "pending"

    admin_agent = await _new_agent(client, ADMIN)
    r = (await client.post(f"/api/agents/{admin_agent}/share-requests", json={"scope": "company"}, headers=h(ADMIN))).json()
    assert r["status"] == "approved"
    assert any(a["agent_id"] == admin_agent for a in (await client.get("/api/agents", headers=h(PARK))).json())


async def test_share_request_rules(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    aid = await _new_agent(client)
    url = f"/api/agents/{aid}/share-requests"
    assert (await client.post(url, json={"scope": "dept"}, headers=h(LEE))).status_code == 403  # 주인만 요청
    assert (await client.post(url, json={"scope": "personal"}, headers=h(KIM))).status_code == 422
    await client.post(f"/api/agents/{aid}/disable", headers=h(KIM))
    assert (await client.post(url, json={"scope": "dept"}, headers=h(KIM))).status_code == 409


async def test_unshare_and_delete_cancel_pending(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    aid = await _new_agent(client)
    rid = (await client.post(f"/api/agents/{aid}/share-requests", json={"scope": "dept"}, headers=h(KIM))).json()["request_id"]
    await client.post(f"/api/share-requests/{rid}/approve", headers=h(LEE))
    rid2 = (await client.post(f"/api/agents/{aid}/share-requests", json={"scope": "company"}, headers=h(KIM))).json()["request_id"]

    assert (await client.post(f"/api/agents/{aid}/unshare", headers=h(CHOI))).status_code == 403
    r = await client.post(f"/api/agents/{aid}/unshare", headers=h(KIM))
    assert r.json()["scope"] == "personal" and r.json()["pending_share_scope"] is None
    assert (await client.get("/api/agents", headers=h(CHOI))).json() == []
    pending = [x for x in (await client.get("/api/share-requests", headers=h(KIM))).json() if x["request_id"] == rid2]
    assert pending[0]["status"] == "rejected"

    rid3 = (await client.post(f"/api/agents/{aid}/share-requests", json={"scope": "dept"}, headers=h(KIM))).json()["request_id"]
    await client.delete(f"/api/agents/{aid}", headers=h(KIM))
    gone = [x for x in (await client.get("/api/share-requests", headers=h(LEE))).json() if x["request_id"] == rid3]
    assert gone[0]["status"] == "rejected" and gone[0]["agent_name"] == "(삭제된 에이전트)"


async def test_shared_or_pending_agent_cannot_be_edited(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    aid = await _new_agent(client)
    url = f"/api/agents/{aid}"
    rid = (await client.post(f"{url}/share-requests", json={"scope": "dept"}, headers=h(KIM))).json()["request_id"]

    # 승인 대기 중: 수정 불가
    r = await client.patch(url, json={"system_prompt": "몰래 바꿈"}, headers=h(KIM))
    assert r.status_code == 409 and "승인 대기" in r.json()["detail"]
    assert (await client.get(url, headers=h(KIM))).json()["can_edit"] is False

    # 공유 중: 주인도 admin 도 수정 불가, 비활성화·삭제 버튼은 그대로
    await client.post(f"/api/share-requests/{rid}/approve", headers=h(LEE))
    for who in (KIM, ADMIN):
        r = await client.patch(url, json={"name": "새 이름"}, headers=h(who))
        assert r.status_code == 409 and "공유를 해제한 뒤" in r.json()["detail"]
    a = (await client.get(url, headers=h(KIM))).json()
    assert (a["can_edit"], a["can_delete"], a["can_disable"], a["can_unshare"]) == (False, True, True, True)

    # 공유를 해제하면 다시 고칠 수 있다
    await client.post(f"{url}/unshare", headers=h(KIM))
    r = await client.patch(url, json={"system_prompt": "고친 역할"}, headers=h(KIM))
    assert r.status_code == 200 and r.json()["can_edit"] is True


# ── 자연어 초안 ─────────────────────────────────────────────

async def test_draft_uses_llm_json_and_drops_unknown_tools(tmp_path):
    reply = '```json\n{"name": "덧셈봇", "description": "더해 준다", "system_prompt": "덧셈만 한다.", "tools": ["add", "send_rocket", "add"]}\n```'
    client, svc = make([{"content": reply}], tmp_path)
    r = await client.post("/api/agents/draft", json={"description": "숫자 더해 주는 에이전트"}, headers=h(KIM))
    d = r.json()
    assert r.status_code == 200
    assert (d["name"], d["tools"], d["dropped_tools"], d["source"]) == ("덧셈봇", ["add"], ["send_rocket"], "llm")
    messages, tools = svc.llm.calls[0]
    assert tools == [] and "add (덧셈)" in messages[0].content
    assert (await client.get("/api/agents", headers=h(KIM))).json() == []  # 저장하지 않는다


async def test_draft_retries_once_then_falls_back(tmp_path):
    client, svc = make([{"content": "음... 이렇게 하면 될 것 같아요"}, {"content": '{"name": "곱셈봇", "system_prompt": "곱한다", "tools": ["multiply"]}'}], tmp_path)
    d = (await client.post("/api/agents/draft", json={"description": "곱해 주는 에이전트"}, headers=h(KIM))).json()
    assert d["name"] == "곱셈봇" and d["source"] == "llm" and len(svc.llm.calls) == 2

    client, _ = make([{"content": "몰라요"}, {"content": "{깨진"}], tmp_path)
    d = (await client.post("/api/agents/draft", json={"description": "덧셈 해주는 에이전트 만들어 줘"}, headers=h(KIM))).json()
    assert d["source"] == "fallback" and d["name"] == "덧셈 해주는" and d["tools"] == ["add"]


async def test_draft_llm_outage_is_502(tmp_path):
    from openai import APIConnectionError

    class Down:
        async def chat(self, messages, tools):
            raise APIConnectionError(request=httpx.Request("POST", "http://llm"))

    client, svc = make(tmp_path=tmp_path)
    svc.llm = Down()
    r = await client.post("/api/agents/draft", json={"description": "아무거나"}, headers=h(KIM))
    assert r.status_code == 502


def test_draft_instruction_keeps_tool_names_out_of_prompt():
    from services.agent_draft import SYSTEM

    assert "툴 이름(영문 name)" in SYSTEM and "쉬운 말" in SYSTEM


def test_base_prompt_hides_internals_and_comes_first(tmp_path):
    from services.chat import system_prompt
    from services.container import DEFAULT_SYSTEM_PROMPT

    assert "chunk_id" in DEFAULT_SYSTEM_PROMPT and "근거:" in DEFAULT_SYSTEM_PROMPT
    _, svc = make(tmp_path=tmp_path)
    text = system_prompt(svc, Agent("a", "kim", 10, "규정봇", "get_doc_chunk 툴을 쓰세요"))
    assert text.index("위 규칙을 따른다") < text.index("규정봇")
