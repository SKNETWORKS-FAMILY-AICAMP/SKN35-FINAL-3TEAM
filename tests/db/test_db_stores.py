"""DB 저장소 (STORE=db) — 실제 DB, 마이그레이션 0009 까지. DB 가 없으면 건너뜀.

저장소는 안에서 commit 하므로 바깥 트랜잭션 하나 안에서 commit 을 savepoint 로 돌리고 끝에 되돌린다
(DB 에 테스트 데이터가 남지 않는다). "서버를 다시 켠 것" 은 저장소 객체를 새로 만들어 확인한다.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from contracts.harness import Denial, LLMToolCall, Message
from contracts.permission import Decision, PolicyRule, Principal
from services.db_stores import REMOVED_AGENT, DbAgentStore, DbSessionStore, DbShareRequestStore, UnknownUser

ROOT = Path(__file__).resolve().parents[2]


def _url() -> str | None:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip() or None
    return None


@pytest_asyncio.fixture
async def db():
    url = _url()
    if not url:
        pytest.skip("DATABASE_URL 없음")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as c:
            if not await c.scalar(text("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                                       "WHERE table_name = 'agent_run' AND column_name = 'denial')")):
                pytest.skip("마이그레이션 0009 전 DB")
            await c.rollback()
            tx = await c.begin()

            @asynccontextmanager
            async def sessions():
                async with AsyncSession(bind=c, expire_on_commit=False,
                                        join_transaction_mode="create_savepoint") as s:
                    yield s

            tag = uuid.uuid4().hex[:6]
            dept = await c.scalar(text("INSERT INTO dept (name) VALUES (:n) RETURNING id"), {"n": f"부서-{tag}"})
            other = await c.scalar(text("INSERT INTO dept (name) VALUES (:n) RETURNING id"), {"n": f"다른-{tag}"})
            people: dict[str, Principal] = {}

            async def person(name: str, role: str, dept_id: int) -> Principal:
                login = f"{name}-{tag}"
                uid = await c.scalar(text("INSERT INTO users (login_id, name, dept_id, security_grade) "
                                          "VALUES (:l, :l, :d, 2) RETURNING id"), {"l": login, "d": dept_id})
                await c.execute(text("INSERT INTO user_role (user_id, role) VALUES (:u, :r)"), {"u": uid, "r": role})
                people[name] = Principal(login, (role,), {"dept_id": dept_id, "security_grade": 2})
                return people[name]

            await person("kim", "member", dept)
            await person("lee", "dept_head", dept)
            await person("park", "member", other)
            await person("boss", "admin", other)
            yield {"sessions": sessions, "conn": c, "p": people}
            await tx.rollback()
    except (OSError, DBAPIError) as e:
        pytest.skip(f"DB 에 붙을 수 없음: {type(e).__name__}")
    finally:
        await engine.dispose()


async def test_agent_crud_and_visibility(db):
    agents, p = DbAgentStore(db["sessions"]), db["p"]
    a = await agents.create(p["kim"], name="규정 도우미", system_prompt="규정을 찾는다", tools=("search_doc", "today"),
                            description="설명")
    assert a.agent_id.isdigit() and a.owner_user_id == p["kim"].user_id and a.scope == "personal"
    assert set(a.tools) == {"search_doc", "today"}

    a = await agents.update(a, tools=("today",), name="새 이름")
    again = await DbAgentStore(db["sessions"]).get(a.agent_id)          # 서버를 다시 켠 것처럼 새 저장소로
    assert again.name == "새 이름" and again.tools == ("today",)

    assert [x.agent_id for x in await agents.visible_to(p["lee"])] == [a.agent_id]   # 같은 부서 부서장
    assert a.agent_id not in [x.agent_id for x in await agents.visible_to(p["park"])]  # 다른 부서 팀원

    a = await agents.update(a, status="disabled", disabled_by=p["lee"].user_id)
    assert (await agents.get(a.agent_id)).disabled_by == p["lee"].user_id
    assert await agents.get("없는-id") is None and await agents.get("999999999") is None


async def test_share_request_flow(db):
    agents, requests, p = DbAgentStore(db["sessions"]), DbShareRequestStore(db["sessions"]), db["p"]
    a = await agents.create(p["kim"], name="공유할 것", system_prompt="x")
    r = await requests.create(a, p["kim"], p["lee"].user_id, "dept", "같이 쓰고 싶어요")
    assert r.status == "pending" and r.approver_id == p["lee"].user_id
    assert (await requests.pending_for_agent(a.agent_id)).request_id == r.request_id
    assert [x.request_id for x in await requests.visible_to(p["lee"])] == [r.request_id]
    assert await requests.visible_to(p["park"]) == []

    r = await requests.decide(r, p["lee"], approved=True, comment="좋아요")
    fresh = await DbShareRequestStore(db["sessions"]).get(r.request_id)
    assert (fresh.status, fresh.decided_by, fresh.decision_comment) == ("approved", p["lee"].user_id, "좋아요")
    assert fresh.decided_at is not None
    assert await requests.pending_for_agent(a.agent_id) is None


async def test_chat_history_survives_restart(db):
    store, p = DbSessionStore(db["sessions"]), db["p"]
    chat = await store.create(p["kim"].user_id)
    turn = [
        Message("user", "3 더하기 4\n두 번째 줄"),
        Message("assistant", "", tool_calls=(LLMToolCall("c1", "add", {"a": 3, "b": 4}),)),
        Message("tool", "7", tool_call_id="c1"),
        Message("assistant", "7입니다"),
    ]
    await store.record_turn(chat, str(uuid.uuid4()), "completed", 2, turn)

    reopened = await DbSessionStore(db["sessions"]).get_owned(chat.session_id, p["kim"].user_id)
    assert reopened.messages == turn
    assert await DbSessionStore(db["sessions"]).get_owned(chat.session_id, p["park"].user_id) is None  # 남의 대화
    assert await store.get_owned("not-a-uuid", p["kim"].user_id) is None

    c = db["conn"]
    title = await c.scalar(text("SELECT title FROM chat_session WHERE id = :i"), {"i": uuid.UUID(chat.session_id)})
    assert title == "3 더하기 4"
    row = (await c.execute(text("SELECT ok, result_content FROM tool_call WHERE call_id = 'c1'"))).first()
    assert tuple(row) == (True, "7")
    run = (await c.execute(text("SELECT llm_calls, stop_reason FROM agent_run WHERE session_id = :i"),
                           {"i": uuid.UUID(chat.session_id)})).first()
    assert tuple(run) == (2, "completed")

    # 권한 거부로 멈춘 두 번째 질문: 거부 내용이 저장되고 다시 열면 몇 번째 질문인지까지 맞다
    denied = Denial("d1", "send_mail", "rejected", "사용자가 거절했습니다", "메일 발송")
    second = [Message("user", "메일 보내 줘"), Message("assistant", "메일은 보내지 않았습니다")]
    chat.messages = [*turn, *second]   # 실제로는 services/chat.py 가 기록 전에 session.messages 를 갱신한다
    await store.record_turn(chat, str(uuid.uuid4()), "denied", 1, second, denial=denied)
    assert chat.denials == {1: denied}
    assert (await DbSessionStore(db["sessions"]).get_owned(chat.session_id, p["kim"].user_id)).denials == {1: denied}

    # 다른 대화에서 같은 call_id 가 와도 저장되고, 다시 읽으면 짝이 맞는다
    other = await store.create(p["kim"].user_id)
    await store.record_turn(other, str(uuid.uuid4()), "completed", 2, turn)
    back = await DbSessionStore(db["sessions"]).get_owned(other.session_id, p["kim"].user_id)
    assert back.messages[1].tool_calls[0].call_id == back.messages[2].tool_call_id != "c1"


async def test_deleted_agent_session_is_not_default_agent(db):
    """에이전트를 지워도 그 대화가 '기본 에이전트(모든 툴)' 로 바뀌면 안 된다."""
    agents, store, p = DbAgentStore(db["sessions"]), DbSessionStore(db["sessions"]), db["p"]
    a = await agents.create(p["kim"], name="잠깐", system_prompt="x")
    chat = await store.create(p["kim"].user_id, a.agent_id)
    await agents.delete(a.agent_id)
    reopened = await DbSessionStore(db["sessions"]).get_owned(chat.session_id, p["kim"].user_id)
    assert reopened.agent_id == REMOVED_AGENT and await agents.get(REMOVED_AGENT) is None


async def test_unknown_user_is_clear_error(db):
    with pytest.raises(UnknownUser):
        await DbAgentStore(db["sessions"]).create(Principal("nobody-xyz"), name="x", system_prompt="y")


async def test_api_with_db_stores(db, tmp_path):
    """API 로 에이전트 만들기 → 공유 요청·승인 → 그 에이전트로 채팅 → 새 저장소(재시작)로 기록 읽기."""
    from main import create_app
    from permission import create_checker
    from permission.approval import FutureApprovalHandler
    from permission.audit import JsonlAuditLogger
    from services.container import AppServices
    from services.users import UserDirectory
    from tests.fakes import ScriptedLLM
    from tools.arithmetic import arithmetic_tools
    from tools.registry import SimpleToolRegistry

    p = db["p"]
    llm = ScriptedLLM([{"tool_calls": [{"id": "x1", "name": "add", "arguments": {"a": 1, "b": 2}}]},
                       {"content": "3입니다"}])
    svc = AppServices(
        llm=llm, tools=SimpleToolRegistry(arithmetic_tools()),
        checker=create_checker(rules=[PolicyRule("math", "add", Decision.ALLOW)], default=Decision.DENY),
        approvals=FutureApprovalHandler(5), audit=JsonlAuditLogger(tmp_path / "a.jsonl"),
        users=UserDirectory({x.user_id: x for x in p.values()}),
        agents=DbAgentStore(db["sessions"]), share_requests=DbShareRequestStore(db["sessions"]),
        sessions=DbSessionStore(db["sessions"]),
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    kim, lee = {"X-User-Id": p["kim"].user_id}, {"X-User-Id": p["lee"].user_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        made = await client.post("/api/agents", headers=kim,
                                 json={"name": "계산", "system_prompt": "계산한다", "tools": ["add"]})
        assert made.status_code == 201, made.text
        agent_id = made.json()["agent_id"]

        req = await client.post(f"/api/agents/{agent_id}/share-requests", headers=kim, json={"scope": "dept"})
        assert req.status_code == 201 and req.json()["approver_id"] == p["lee"].user_id
        ok = await client.post(f"/api/share-requests/{req.json()['request_id']}/approve", headers=lee,
                               json={"comment": "승인"})
        assert ok.json()["status"] == "approved"
        assert (await client.get(f"/api/agents/{agent_id}", headers=lee)).json()["scope"] == "dept"

        chat = await client.post("/api/chat/messages", headers=kim, json={"content": "1+2", "agent_id": agent_id})
        assert chat.status_code == 200 and chat.json()["message"] == "3입니다"
        session_id = chat.json()["session_id"]

    svc.sessions = DbSessionStore(db["sessions"])   # 서버 재시작: 메모리 캐시 없이 DB 에서 읽는다
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        history = (await client.get(f"/api/chat/sessions/{session_id}/messages", headers=kim)).json()
        assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
        assert history[-1]["content"] == "3입니다"
        assert (await client.get(f"/api/chat/sessions/{session_id}/messages", headers=lee)).status_code == 404

        # 최근 대화 목록과 다시 열기 (화면 왼쪽 목록)
        listed = (await client.get("/api/chat/sessions", headers=kim)).json()
        assert [(x["session_id"], x["title"], x["agent_id"]) for x in listed] == [(session_id, "1+2", agent_id)]
        assert listed[0]["updated_at"]
        assert (await client.get("/api/chat/sessions", headers=lee)).json() == []
        turns = (await client.get(f"/api/chat/sessions/{session_id}/turns", headers=kim)).json()
        assert [(t["question"], t["message"]) for t in turns] == [("1+2", "3입니다")]
        assert [(e["tool_name"], e["result"], e["ok"]) for e in turns[0]["tool_events"]] == [("add", "3", True)]
