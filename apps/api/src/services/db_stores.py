"""DB 저장소 — 에이전트·공유 요청·대화를 PostgreSQL 에 둔다 (STORE=db).

메모리 저장소(services/agents.py, services/sessions.py)와 메서드·결과 모양이 같다.
권한 판단(can_view 등)은 그대로 services/agents.py 의 규칙을 쓴다 (DB 는 저장만).

표 (팀 ERD + 0008)
  agent · agent_tool                  에이전트와 쓰는 툴
  agent_share_request                 공유 요청 (decided_by · decision_comment 는 0008)
  chat_session · message · tool_call · agent_run   대화 기록. 요청 1건 = agent_run 1줄

ID 는 DB 번호를 문자열로 쓴다 (에이전트 "12", 공유 요청 "3", 대화는 UUID 문자열).
사람은 앱 전체가 login_id(예: kim)로 다루고, DB 에는 users.id 로 넣는다 → users 표에 있는 사람이어야 한다.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.harness import Denial, LLMToolCall, Message, StopReason
from contracts.permission import Principal
from services.agents import Agent, SharedScope, ShareRequest, can_see_request, can_view
from services.sessions import ChatSession, SessionSummary, turn_index

log = logging.getLogger("uvicorn.error")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

REMOVED_AGENT = "removed"   # 삭제된 에이전트로 하던 대화의 agent_id (숫자가 아니라 어떤 에이전트로도 안 찾아짐)


class UnknownUser(ValueError):
    """users 표에 없는 사람 (개발 모드라면 scripts/db/seed_users.py 를 먼저 돌린다)."""


def _num(value: str | None) -> int | None:
    return int(value) if value is not None and str(value).isdigit() else None


async def _user_pk(s: AsyncSession, login_id: str) -> int:
    pk = await s.scalar(text("SELECT id FROM users WHERE login_id = :l"), {"l": login_id})
    if pk is None:
        raise UnknownUser(f"DB users 표에 없는 사용자입니다: {login_id} (scripts/db/seed_users.py 로 넣으세요)")
    return pk


# ── 에이전트 ───────────────────────────────────────────

_AGENT_SQL = """
    SELECT a.id, o.login_id AS owner, o.dept_id AS owner_dept, a.name, a.description, a.system_prompt,
           a.scope, a.status, d.login_id AS disabled_by,
           COALESCE(array_agg(t.tool_name ORDER BY t.tool_name) FILTER (WHERE t.tool_name IS NOT NULL), '{}') AS tools
    FROM agent a
    JOIN users o ON o.id = a.owner_user_id
    LEFT JOIN users d ON d.id = a.disabled_by
    LEFT JOIN agent_tool t ON t.agent_id = a.id
"""
_AGENT_GROUP = " GROUP BY a.id, o.login_id, o.dept_id, d.login_id"


def _agent(row: Any) -> Agent:
    return Agent(
        agent_id=str(row.id), owner_user_id=row.owner, owner_dept_id=row.owner_dept, name=row.name,
        system_prompt=row.system_prompt, tools=tuple(row.tools or ()), description=row.description or "",
        scope=row.scope, status=row.status, disabled_by=row.disabled_by,
    )


class DbAgentStore:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def create(self, owner: Principal, *, name: str, system_prompt: str,
                     tools: tuple[str, ...] = (), description: str = "") -> Agent:
        async with self._sessions() as s:
            pk = await s.scalar(text("""
                INSERT INTO agent (owner_user_id, name, description, system_prompt)
                VALUES (:o, :n, :d, :p) RETURNING id"""),
                {"o": await _user_pk(s, owner.user_id), "n": name, "d": description, "p": system_prompt})
            await self._set_tools(s, pk, tools)
            await s.commit()
        return await self.get(str(pk))

    async def get(self, agent_id: str) -> Agent | None:
        pk = _num(agent_id)
        if pk is None:
            return None
        async with self._sessions() as s:
            row = (await s.execute(text(_AGENT_SQL + " WHERE a.id = :i" + _AGENT_GROUP), {"i": pk})).first()
        return _agent(row) if row else None

    async def visible_to(self, p: Principal) -> list[Agent]:
        async with self._sessions() as s:
            rows = (await s.execute(text(_AGENT_SQL + _AGENT_GROUP + " ORDER BY a.id"))).all()
        return [a for a in map(_agent, rows) if can_view(p, a)]

    async def save(self, agent: Agent) -> Agent:
        pk = _num(agent.agent_id)
        async with self._sessions() as s:
            disabled_by = await _user_pk(s, agent.disabled_by) if agent.disabled_by else None
            await s.execute(text("""
                UPDATE agent SET name = :n, description = :d, system_prompt = :p, scope = :sc, status = :st,
                                 disabled_by = :db, updated_at = now()
                WHERE id = :i"""),
                {"n": agent.name, "d": agent.description, "p": agent.system_prompt, "sc": agent.scope,
                 "st": agent.status, "db": disabled_by, "i": pk})
            await self._set_tools(s, pk, agent.tools)
            await s.commit()
        return await self.get(agent.agent_id) or agent

    async def update(self, agent: Agent, **changes: object) -> Agent:
        return await self.save(replace(agent, **changes))

    async def delete(self, agent_id: str) -> None:
        pk = _num(agent_id)
        if pk is None:
            return
        async with self._sessions() as s:
            # 이 에이전트로 하던 대화는 기록은 남기되 더 이어가지 못하게 표시 (agent_id 는 FK 로 NULL 이 된다)
            await s.execute(text("UPDATE chat_session SET agent_removed = TRUE WHERE agent_id = :i"), {"i": pk})
            await s.execute(text("DELETE FROM agent WHERE id = :i"), {"i": pk})
            await s.commit()

    @staticmethod
    async def _set_tools(s: AsyncSession, pk: int, tools: tuple[str, ...]) -> None:
        await s.execute(text("DELETE FROM agent_tool WHERE agent_id = :i"), {"i": pk})
        for name in dict.fromkeys(tools):
            await s.execute(text("INSERT INTO agent_tool (agent_id, tool_name) VALUES (:i, :t)"), {"i": pk, "t": name})


# ── 공유 요청 ──────────────────────────────────────────

_REQUEST_SQL = """
    SELECT r.id, r.agent_id, rq.login_id AS requester, ap.login_id AS approver, r.requested_scope, r.status,
           r.comment, dc.login_id AS decided_by, r.decision_comment, r.requested_at, r.decided_at
    FROM agent_share_request r
    JOIN users rq ON rq.id = r.requester_id
    JOIN users ap ON ap.id = r.approver_id
    LEFT JOIN users dc ON dc.id = r.decided_by
"""


def _request(row: Any) -> ShareRequest:
    return ShareRequest(
        request_id=str(row.id), agent_id=str(row.agent_id), requester_id=row.requester, approver_id=row.approver,
        requested_scope=row.requested_scope, comment=row.comment or "", status=row.status,
        decided_by=row.decided_by, decision_comment=row.decision_comment or "",
        requested_at=row.requested_at, decided_at=row.decided_at,
    )


class DbShareRequestStore:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def create(self, agent: Agent, requester: Principal, approver_id: str, scope: SharedScope,
                     comment: str) -> ShareRequest:
        async with self._sessions() as s:
            pk = await s.scalar(text("""
                INSERT INTO agent_share_request (agent_id, requester_id, approver_id, requested_scope, comment)
                VALUES (:a, :rq, :ap, :sc, :c) RETURNING id"""),
                {"a": _num(agent.agent_id), "rq": await _user_pk(s, requester.user_id),
                 "ap": await _user_pk(s, approver_id), "sc": scope, "c": comment})
            await s.commit()
        return await self.get(str(pk))

    async def get(self, request_id: str) -> ShareRequest | None:
        pk = _num(request_id)
        if pk is None:
            return None
        async with self._sessions() as s:
            row = (await s.execute(text(_REQUEST_SQL + " WHERE r.id = :i"), {"i": pk})).first()
        return _request(row) if row else None

    async def pending_for_agent(self, agent_id: str) -> ShareRequest | None:
        pk = _num(agent_id)
        if pk is None:
            return None
        async with self._sessions() as s:
            row = (await s.execute(text(_REQUEST_SQL + " WHERE r.agent_id = :a AND r.status = 'pending' "
                                        "ORDER BY r.id DESC LIMIT 1"), {"a": pk})).first()
        return _request(row) if row else None

    async def visible_to(self, p: Principal) -> list[ShareRequest]:
        async with self._sessions() as s:
            rows = (await s.execute(text(_REQUEST_SQL + " ORDER BY r.requested_at DESC, r.id DESC"))).all()
        return [r for r in map(_request, rows) if can_see_request(p, r)]

    async def decide(self, r: ShareRequest, by: Principal, approved: bool, comment: str = "") -> ShareRequest:
        async with self._sessions() as s:
            await s.execute(text("""
                UPDATE agent_share_request SET status = :st, decided_by = :by, decision_comment = :c, decided_at = now()
                WHERE id = :i"""),
                {"st": "approved" if approved else "rejected", "by": await _user_pk(s, by.user_id), "c": comment,
                 "i": _num(r.request_id)})
            await s.commit()
        return await self.get(r.request_id) or r

    async def cancel_pending(self, agent_id: str, by: Principal, reason: str) -> None:
        r = await self.pending_for_agent(agent_id)
        if r is not None:
            await self.decide(r, by, approved=False, comment=reason)


# ── 대화 ───────────────────────────────────────────────

TITLE_LENGTH = 80


class DbSessionStore:
    """대화 기록을 DB 에 둔다. 같은 대화에 요청이 겹치지 않게 하는 lock 은 서버 안에 둬야 해서
    한 번 연 대화는 메모리에도 들고 있는다 (기록의 원본은 DB)."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions
        self._open: dict[str, ChatSession] = {}

    async def create(self, user_id: str, agent_id: str | None = None) -> ChatSession:
        async with self._sessions() as s:
            sid = await s.scalar(text("INSERT INTO chat_session (user_id, agent_id) VALUES (:u, :a) RETURNING id"),
                                 {"u": await _user_pk(s, user_id), "a": _num(agent_id)})
            await s.commit()
        chat = ChatSession(str(sid), user_id, agent_id)
        self._open[chat.session_id] = chat
        return chat

    async def get_owned(self, session_id: str, user_id: str) -> ChatSession | None:
        """본인 대화만 돌려준다. 남의 대화·없는 대화는 None (있는지도 알려주지 않음)."""
        chat = self._open.get(session_id)
        if chat is not None:
            return chat if chat.user_id == user_id else None
        try:
            sid = uuid.UUID(session_id)
        except (ValueError, TypeError):
            return None
        async with self._sessions() as s:
            row = (await s.execute(text("""
                SELECT cs.id, u.login_id, cs.agent_id, cs.agent_removed
                FROM chat_session cs JOIN users u ON u.id = cs.user_id WHERE cs.id = :i"""), {"i": sid})).first()
            if row is None or row.login_id != user_id:
                return None
            messages = await self._load_messages(s, sid)
            denials = await self._load_denials(s, sid)
        agent_id = REMOVED_AGENT if row.agent_removed else (str(row.agent_id) if row.agent_id is not None else None)
        chat = ChatSession(str(row.id), user_id, agent_id, messages, denials=denials)
        self._open[chat.session_id] = chat
        return chat

    async def list_owned(self, user_id: str) -> list[ChatSession]:
        async with self._sessions() as s:
            ids = (await s.execute(text("""
                SELECT cs.id FROM chat_session cs JOIN users u ON u.id = cs.user_id
                WHERE u.login_id = :l ORDER BY cs.updated_at DESC"""), {"l": user_id})).scalars().all()
        found = [await self.get_owned(str(i), user_id) for i in ids]
        return [c for c in found if c is not None]

    async def summaries(self, user_id: str) -> list[SessionSummary]:
        """메시지가 있는 내 대화, 최근 것부터 (제목·시각만 읽고 본문은 안 읽는다)."""
        async with self._sessions() as s:
            rows = (await s.execute(text("""
                SELECT cs.id, cs.agent_id, cs.agent_removed, cs.title, cs.updated_at
                FROM chat_session cs JOIN users u ON u.id = cs.user_id
                WHERE u.login_id = :l AND EXISTS (SELECT 1 FROM message m WHERE m.session_id = cs.id)
                ORDER BY cs.updated_at DESC"""), {"l": user_id})).all()
        return [SessionSummary(
            str(r.id), REMOVED_AGENT if r.agent_removed else (str(r.agent_id) if r.agent_id is not None else None),
            r.title or "", r.updated_at) for r in rows]

    async def record_turn(self, session: ChatSession, run_id: str, stop_reason: StopReason, llm_calls: int,
                          new_messages: list[Message], denial: Denial | None = None) -> None:
        """요청 1건(agent_run)과 그 안의 새 메시지·툴 호출을 한 번에 저장한다. 권한 거부로 멈췄으면 거부 내용도."""
        sid = uuid.UUID(session.session_id)
        denial_json = None
        if denial is not None:
            session.denials[turn_index(session)] = denial
            denial_json = json.dumps({"call_id": denial.call_id, "tool_name": denial.tool_name,
                                      "display_name": denial.display_name, "kind": denial.kind,
                                      "reason": denial.reason}, ensure_ascii=False)
        async with self._sessions() as s:
            await s.execute(text("""
                INSERT INTO agent_run (id, session_id, llm_calls, stop_reason, ended_at, denial)
                VALUES (:r, :s, :n, :st, now(), CAST(:d AS JSONB))"""),
                {"r": uuid.UUID(run_id), "s": sid, "n": llm_calls, "st": stop_reason, "d": denial_json})
            seq = await s.scalar(text("SELECT COALESCE(max(seq), 0) FROM message WHERE session_id = :s"), {"s": sid})
            stored_ids: dict[str, str] = {}   # 하네스 call_id → DB call_id (다른 대화와 겹치면 바꿔 저장)
            for m in new_messages:
                if m.role == "system":
                    continue
                seq += 1
                tool_call_id = stored_ids.get(m.tool_call_id, m.tool_call_id) if m.tool_call_id else None
                mid = await s.scalar(text("""
                    INSERT INTO message (session_id, run_id, seq, role, content, tool_call_id)
                    VALUES (:s, :r, :q, :role, :c, :t) RETURNING id"""),
                    {"s": sid, "r": uuid.UUID(run_id), "q": seq, "role": m.role, "c": m.content, "t": tool_call_id})
                for i, tc in enumerate(m.tool_calls, start=1):
                    call_id = tc.call_id
                    if await s.scalar(text("SELECT 1 FROM tool_call WHERE call_id = :c"), {"c": call_id}):
                        call_id = f"{tc.call_id}@{run_id[:8]}"
                    stored_ids[tc.call_id] = call_id
                    await s.execute(text("""
                        INSERT INTO tool_call (call_id, message_id, seq, tool_name, arguments)
                        VALUES (:c, :m, :q, :n, CAST(:a AS JSONB))"""),
                        {"c": call_id, "m": mid, "q": i, "n": tc.tool_name,
                         "a": json.dumps(tc.arguments, ensure_ascii=False, default=str)})
                if m.role == "tool" and tool_call_id:
                    await s.execute(text("UPDATE tool_call SET result_content = :c, ok = :ok WHERE call_id = :t"),
                                    {"c": m.content, "ok": not m.content.startswith("error:"), "t": tool_call_id})
            first_user = next((m.content for m in new_messages if m.role == "user"), "")
            if first_user:
                await s.execute(text("UPDATE chat_session SET title = :t WHERE id = :s AND title IS NULL"),
                                {"t": first_user.strip().splitlines()[0][:TITLE_LENGTH] if first_user.strip() else None,
                                 "s": sid})
            await s.commit()

    @staticmethod
    async def _load_denials(s: AsyncSession, sid: uuid.UUID) -> dict[int, Denial]:
        """거부로 멈춘 요청마다 그 요청이 몇 번째 질문(0부터)인지 → 거부 내용."""
        rows = (await s.execute(text("""
            SELECT ar.denial,
                   (SELECT count(*) FROM message u
                    WHERE u.session_id = ar.session_id AND u.role = 'user' AND u.seq <= m.seq) - 1 AS turn
            FROM agent_run ar JOIN message m ON m.run_id = ar.id AND m.role = 'user'
            WHERE ar.session_id = :s AND ar.denial IS NOT NULL"""), {"s": sid})).all()
        out = {}
        for r in rows:
            d = r.denial if isinstance(r.denial, dict) else json.loads(r.denial)
            out[int(r.turn)] = Denial(str(d.get("call_id", "")), str(d.get("tool_name", "")), d.get("kind", "deny"),
                                      str(d.get("reason", "")), str(d.get("display_name", "")))
        return out

    @staticmethod
    async def _load_messages(s: AsyncSession, sid: uuid.UUID) -> list[Message]:
        rows = (await s.execute(text("""
            SELECT id, role, content, tool_call_id FROM message WHERE session_id = :s ORDER BY seq"""),
            {"s": sid})).all()
        calls: dict[int, list[LLMToolCall]] = {}
        if rows:
            for c in (await s.execute(text("""
                SELECT message_id, call_id, tool_name, arguments FROM tool_call
                WHERE message_id = ANY(:ids) ORDER BY message_id, seq"""), {"ids": [r.id for r in rows]})).all():
                args = c.arguments if isinstance(c.arguments, dict) else json.loads(c.arguments or "{}")
                calls.setdefault(c.message_id, []).append(LLMToolCall(c.call_id, c.tool_name, args))
        return [Message(role=r.role, content=r.content or "", tool_calls=tuple(calls.get(r.id, ())),
                        tool_call_id=r.tool_call_id) for r in rows]
