"""로그인 — 실제 DB (마이그레이션 0007 까지). DB 가 없으면 건너뜀.

AuthService 는 안에서 commit 하므로, 바깥 트랜잭션 하나 안에서 commit 을 savepoint 로 바꿔 돌리고
끝에 바깥 트랜잭션을 되돌린다 → DB 에 사용자·세션·감사 기록이 남지 않는다.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from main import create_app
from services.auth import AuthService, AuthSettings, DbUserDirectory, LoginError, hash_password

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "test-pass-1234"


def _url() -> str | None:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip() or None
    return None


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t


@pytest_asyncio.fixture
async def env():
    url = _url()
    if not url:
        pytest.skip("DATABASE_URL 없음")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as c:
            if not await c.scalar(text("SELECT to_regclass('login_session') IS NOT NULL")):
                pytest.skip("마이그레이션 0007 전 DB")
            await c.rollback()
            tx = await c.begin()

            @asynccontextmanager
            async def sessions():
                async with AsyncSession(bind=c, expire_on_commit=False,
                                        join_transaction_mode="create_savepoint") as s:
                    yield s

            tag = uuid.uuid4().hex[:8]
            dept = await c.scalar(text("INSERT INTO dept (name) VALUES (:n) RETURNING id"), {"n": f"테스트부서-{tag}"})

            async def add_user(login: str, roles=("member",), grade=2, password=PASSWORD, active=True) -> str:
                login = f"{login}-{tag}"
                uid = await c.scalar(text("""
                    INSERT INTO users (login_id, name, dept_id, position, security_grade, password_hash, is_active)
                    VALUES (:l, :n, :d, '사원', :g, :h, :a) RETURNING id"""),
                    {"l": login, "n": f"이름-{login}", "d": dept, "g": grade,
                     "h": hash_password(password) if password else None, "a": active})
                for r in roles:
                    await c.execute(text("INSERT INTO user_role (user_id, role) VALUES (:u, :r)"), {"u": uid, "r": r})
                return login

            clock = Clock()
            users = DbUserDirectory(sessions)
            auth = AuthService(sessions, users, AuthSettings(max_fails=3, lock_minutes=10, session_hours=12), now=clock)
            yield {"conn": c, "auth": auth, "add_user": add_user, "clock": clock, "dept": dept}
            await tx.rollback()
    except (OSError, DBAPIError) as e:
        pytest.skip(f"DB 에 붙을 수 없음: {type(e).__name__}")
    finally:
        await engine.dispose()


async def _audit(c, login: str) -> list[str]:
    rows = await c.execute(text("""
        SELECT a.event_type FROM audit_log a JOIN users u ON u.id = a.user_id
        WHERE u.login_id = :l ORDER BY a.id"""), {"l": login})
    return [r[0] for r in rows]


async def test_login_resolve_logout(env):
    auth, c = env["auth"], env["conn"]
    kim = await env["add_user"]("kim", grade=3)
    token, p = await auth.login(kim, PASSWORD, client_ip="10.0.0.5", user_agent="pytest")
    assert p.user_id == kim and p.roles == ("member",)
    assert p.attributes["dept_id"] == env["dept"] and p.attributes["security_grade"] == 3

    stored = await c.scalar(text("SELECT count(*) FROM login_session WHERE token_hash = :t"), {"t": token})
    assert stored == 0                               # 토큰 원문은 DB 에 없다 (해시만)
    assert (await auth.resolve(token)).user_id == kim

    await auth.logout(token)
    assert await auth.resolve(token) is None         # 로그아웃한 토큰은 못 쓴다
    assert await _audit(c, kim) == ["login", "logout"]


async def test_attribute_change_applies_on_next_request(env):
    auth, c = env["auth"], env["conn"]
    kim = await env["add_user"]("kim", grade=2)
    token, _ = await auth.login(kim, PASSWORD)
    await c.execute(text("UPDATE users SET security_grade = 4 WHERE login_id = :l"), {"l": kim})
    assert (await auth.resolve(token)).attributes["security_grade"] == 4


async def test_wrong_password_and_unknown_user_same_message(env):
    auth = env["auth"]
    kim = await env["add_user"]("kim")
    with pytest.raises(LoginError) as wrong:
        await auth.login(kim, "nope")
    with pytest.raises(LoginError) as unknown:
        await auth.login(f"ghost-{uuid.uuid4().hex[:6]}", "nope")
    assert str(wrong.value) == str(unknown.value) and wrong.value.status == unknown.value.status == 401


async def test_lock_after_repeated_failures_then_unlock(env):
    auth, c, clock = env["auth"], env["conn"], env["clock"]
    kim = await env["add_user"]("kim")
    for _ in range(2):
        with pytest.raises(LoginError):
            await auth.login(kim, "nope")
    with pytest.raises(LoginError) as locked:        # 3번째 실패 → 잠김
        await auth.login(kim, "nope")
    assert locked.value.status == 429
    with pytest.raises(LoginError) as still:         # 맞는 비밀번호라도 잠긴 동안 거부
        await auth.login(kim, PASSWORD)
    assert still.value.status == 429

    clock.t += timedelta(minutes=11)                 # 잠금 시간이 지나면 다시 된다
    token, _ = await auth.login(kim, PASSWORD)
    assert await auth.resolve(token) is not None
    fails = await c.scalar(text("SELECT failed_login_count FROM users WHERE login_id = :l"), {"l": kim})
    assert fails == 0
    assert (await _audit(c, kim)).count("login_failed") == 4


async def test_inactive_or_passwordless_user_cannot_login(env):
    auth = env["auth"]
    gone = await env["add_user"]("gone", active=False)
    sso = await env["add_user"]("sso", password=None)
    for login in (gone, sso):
        with pytest.raises(LoginError):
            await auth.login(login, PASSWORD)


async def test_deactivation_and_expiry_end_sessions(env):
    auth, c, clock = env["auth"], env["conn"], env["clock"]
    kim = await env["add_user"]("kim")
    lee = await env["add_user"]("lee")
    t_kim, _ = await auth.login(kim, PASSWORD)
    t_lee, _ = await auth.login(lee, PASSWORD)

    await c.execute(text("UPDATE users SET is_active = FALSE WHERE login_id = :l"), {"l": kim})
    assert await auth.resolve(t_kim) is None         # 정지되면 이미 로그인한 세션도 끊긴다

    clock.t += timedelta(hours=13)                   # 12시간 지나면 만료
    assert await auth.resolve(t_lee) is None


async def test_revoke_all(env):
    auth = env["auth"]
    kim = await env["add_user"]("kim")
    t1, _ = await auth.login(kim, PASSWORD)
    t2, _ = await auth.login(kim, PASSWORD)
    assert await auth.revoke_all(kim) == 2
    assert await auth.resolve(t1) is None and await auth.resolve(t2) is None


async def test_superior_lookup_from_db(env):
    auth = env["auth"]
    kim = await env["add_user"]("kim")
    head = await env["add_user"]("head", roles=("dept_head",))
    await auth.users.reload()
    assert auth.users.superior_of(auth.users.get(kim)).user_id == head


async def test_api_session_mode(env, tmp_path):
    """API 로: 로그인 → 쿠키 → /api/me, X-User-Id 무시, 개발용 목록 막힘, 로그아웃 → 401."""
    from contracts.permission import Decision
    from permission import create_checker
    from permission.approval import FutureApprovalHandler
    from permission.audit import JsonlAuditLogger
    from services.container import AppServices
    from tests.fakes import ScriptedLLM
    from tools.registry import SimpleToolRegistry

    auth = env["auth"]
    kim = await env["add_user"]("kim", grade=3)
    svc = AppServices(llm=ScriptedLLM([]), tools=SimpleToolRegistry([]), checker=create_checker(rules=[], default=Decision.DENY),
                      approvals=FutureApprovalHandler(5), audit=JsonlAuditLogger(tmp_path / "a.jsonl"),
                      users=auth.users, auth=auth)
    app = create_app(build=lambda: svc)
    app.state.services = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/auth/mode")).json() == {"mode": "session"}
        assert (await client.get("/api/me", headers={"X-User-Id": "admin"})).status_code == 401  # 헤더로는 못 들어옴
        assert (await client.get("/api/dev/users")).status_code == 404

        bad = await client.post("/api/auth/login", json={"login_id": kim, "password": "nope"})
        assert bad.status_code == 401

        ok = await client.post("/api/auth/login", json={"login_id": kim, "password": PASSWORD})
        assert ok.status_code == 200 and ok.json()["user_id"] == kim
        assert "user_pk" not in ok.json()["attributes"]
        cookie = ok.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie

        me = await client.get("/api/me")
        assert me.status_code == 200 and me.json()["attributes"]["security_grade"] == 3

        assert (await client.post("/api/auth/logout")).status_code == 204
        assert (await client.get("/api/me")).status_code == 401
