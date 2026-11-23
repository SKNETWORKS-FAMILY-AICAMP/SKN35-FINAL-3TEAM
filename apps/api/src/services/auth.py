"""로그인 (COM-01) — 아이디·비밀번호 확인, 로그인 세션, DB 사용자 조회.

AUTH_MODE=session 일 때만 쓴다 (기본 dev 는 지금처럼 X-User-Id 헤더 + config/dev_users.yaml).

흐름
  1) POST /api/auth/login  → 비밀번호 확인 → 임의 토큰을 HttpOnly 쿠키로 준다
     DB login_session 에는 토큰의 SHA-256 만 둔다 (DB 가 새도 쿠키를 만들 수 없게)
  2) 이후 요청          → 쿠키 토큰 → 세션 → users·user_role 에서 Principal 을 그때그때 채운다
     (부서·등급을 바꾸면 다음 요청부터 바로 반영. 클라이언트가 보낸 속성은 믿지 않는다)
  3) POST /api/auth/logout → revoked_at 을 채워 그 토큰을 못 쓰게 한다

보안 규칙
  - 비밀번호는 scrypt 해시로만 저장 (외부 패키지 없이 hashlib). 원문은 어디에도 남기지 않는다
  - 없는 아이디·틀린 비밀번호·정지 계정은 같은 문구로 거절 (계정 존재 여부를 숨김)
  - 연속 실패 AUTH_MAX_FAILS 회 → AUTH_LOCK_MINUTES 분 잠금
  - 로그인 성공·실패·로그아웃을 audit_log 에 남긴다 (없는 아이디는 user_id 가 없어 서버 로그에만)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.permission import Principal
from services.users import UserDirectory

log = logging.getLogger("uvicorn.error")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# ── 비밀번호 해시 ───────────────────────────────────────

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1   # 메모리 약 32MB, 한 번에 0.1초 안팎
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    """'scrypt$N$r$p$소금$해시' 형식. 같은 비밀번호도 소금이 달라 매번 다른 값."""
    if not password:
        raise ValueError("빈 비밀번호는 쓸 수 없습니다")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, expected = (stored or "").split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            maxmem=_MAXMEM, dklen=len(base64.b64decode(expected)))
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


# 없는 아이디일 때도 같은 시간만큼 계산해 응답 시간으로 계정 존재를 알 수 없게 한다
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── 오류 ───────────────────────────────────────────────

class LoginError(Exception):
    """로그인 거절. status 는 HTTP 상태 (401 또는 429)."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


BAD_CREDENTIALS = "사번 또는 비밀번호가 올바르지 않습니다."


# ── DB 사용자 목록 (부서장·관리자 찾기용) ─────────────────

_USER_SQL = """
    SELECT u.id, u.login_id, u.name, u.dept_id, u.position, u.security_grade, u.is_active,
           u.password_hash, u.failed_login_count, u.locked_until,
           COALESCE(array_agg(r.role ORDER BY r.role) FILTER (WHERE r.role IS NOT NULL), '{}') AS roles
    FROM users u LEFT JOIN user_role r ON r.user_id = u.id
"""

# 유효한 세션(로그아웃·만료 아님, 계정 사용 중)의 사용자 한 명
_SESSION_SQL = """
    SELECT ls.last_seen_at, u.id, u.login_id, u.name, u.dept_id, u.position, u.security_grade,
           COALESCE(array_agg(r.role ORDER BY r.role) FILTER (WHERE r.role IS NOT NULL), '{}') AS roles
    FROM login_session ls
    JOIN users u ON u.id = ls.user_id
    LEFT JOIN user_role r ON r.user_id = u.id
    WHERE ls.token_hash = :h AND ls.revoked_at IS NULL AND ls.expires_at > :now AND u.is_active
    GROUP BY ls.token_hash, u.id
"""


def principal_from_row(row: Any) -> Principal:
    """users 한 줄 → Principal. user_id 는 지금 코드 전체가 쓰는 login_id (예: kim)."""
    return Principal(
        user_id=row.login_id,
        roles=tuple(row.roles or ()),
        attributes={
            "dept_id": row.dept_id,
            "position": row.position,
            "security_grade": row.security_grade,
            "name": row.name,
            "user_pk": row.id,   # users.id (감사 로그·DB 외래키용)
        },
    )


class DbUserDirectory(UserDirectory):
    """UserDirectory 와 같은 모양 (get · ids · superior_of 는 동기). 값은 DB 에서 reload() 로 채운다.

    로그인한 사람의 속성은 요청마다 DB 에서 새로 읽고, 이 목록은 '바로 위 관리자 찾기' 같은
    다른 사람 조회에만 쓴다. 서버 시작·로그인 때 다시 읽는다.
    """

    def __init__(self, sessions: SessionFactory) -> None:
        super().__init__({})
        self._sessions = sessions

    async def reload(self) -> None:
        async with self._sessions() as s:
            rows = (await s.execute(text(_USER_SQL + " WHERE u.is_active GROUP BY u.id ORDER BY u.id"))).all()
        self._users = {r.login_id: principal_from_row(r) for r in rows}


# ── 로그인 서비스 ───────────────────────────────────────

@dataclass
class AuthSettings:
    cookie_name: str = "argus_session"
    session_hours: float = 12
    max_fails: int = 5
    lock_minutes: float = 10
    cookie_secure: bool = False   # HTTPS 로 서비스하면 true

    @classmethod
    def from_env(cls) -> AuthSettings:
        env = os.environ.get
        return cls(
            cookie_name=env("AUTH_COOKIE_NAME", "argus_session"),
            session_hours=float(env("AUTH_SESSION_HOURS", "12")),
            max_fails=int(env("AUTH_MAX_FAILS", "5")),
            lock_minutes=float(env("AUTH_LOCK_MINUTES", "10")),
            cookie_secure=env("AUTH_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes"),
        )


class AuthService:
    def __init__(self, sessions: SessionFactory, users: DbUserDirectory, settings: AuthSettings | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._sessions = sessions
        self.users = users
        self.settings = settings or AuthSettings()
        self._now = now

    @property
    def cookie_name(self) -> str:
        return self.settings.cookie_name

    async def startup(self) -> None:
        """서버 시작 때: DB 연결 확인 겸 사용자 목록 읽기. 실패하면 서버가 뜨지 않게 그대로 올린다."""
        await self.users.reload()
        log.info("로그인 모드(session): 사용자 %d명", len(self.users.ids()))

    async def login(self, login_id: str, password: str, client_ip: str | None = None,
                    user_agent: str | None = None) -> tuple[str, Principal]:
        login_id = login_id.strip()
        now = self._now()
        async with self._sessions() as s:
            row = (await s.execute(text(_USER_SQL + " WHERE u.login_id = :l GROUP BY u.id"), {"l": login_id})).first()
            if row is None:
                verify_password(password, _DUMMY_HASH)
                log.warning("로그인 실패: 없는 아이디 %r (ip=%s)", login_id[:100], client_ip)
                raise LoginError(BAD_CREDENTIALS)
            if row.locked_until is not None and row.locked_until > now:
                minutes = max(1, int((row.locked_until - now).total_seconds() // 60) + 1)
                await self._audit(s, "login_failed", row, f"잠긴 계정 (ip={client_ip})")
                await s.commit()
                raise LoginError(f"로그인 실패가 너무 많아 잠겼습니다. {minutes}분 뒤에 다시 시도해 주세요.", 429)
            ok = verify_password(password, row.password_hash)
            if not ok or not row.is_active:
                fails = row.failed_login_count + 1
                locked = now + timedelta(minutes=self.settings.lock_minutes) if fails >= self.settings.max_fails else None
                await s.execute(text("UPDATE users SET failed_login_count = :f, locked_until = :t WHERE id = :i"),
                                {"f": 0 if locked else fails, "t": locked, "i": row.id})
                why = "정지된 계정" if (ok and not row.is_active) else f"비밀번호 불일치 {fails}회"
                await self._audit(s, "login_failed", row, f"{why}{' → 잠금' if locked else ''} (ip={client_ip})")
                await s.commit()
                if locked:
                    raise LoginError(f"로그인 실패가 너무 많아 {int(self.settings.lock_minutes)}분 동안 잠겼습니다.", 429)
                raise LoginError(BAD_CREDENTIALS)

            token = secrets.token_urlsafe(32)
            await s.execute(text("""
                INSERT INTO login_session (token_hash, user_id, created_at, expires_at, last_seen_at, client_ip, user_agent)
                VALUES (:h, :u, :now, :exp, :now, :ip, :ua)"""),
                {"h": token_digest(token), "u": row.id, "now": now,
                 "exp": now + timedelta(hours=self.settings.session_hours),
                 "ip": (client_ip or "")[:64] or None, "ua": (user_agent or "")[:300] or None})
            await s.execute(text("UPDATE users SET failed_login_count = 0, locked_until = NULL, last_login_at = :n "
                                 "WHERE id = :i"), {"n": now, "i": row.id})
            await self._audit(s, "login", row, f"ip={client_ip}")
            await s.commit()
        await self.users.reload()
        return token, principal_from_row(row)

    async def resolve(self, token: str) -> Principal | None:
        """쿠키 토큰 → Principal. 없거나·만료·로그아웃·정지 계정이면 None."""
        if not token or len(token) > 200:
            return None
        now = self._now()
        async with self._sessions() as s:
            row = (await s.execute(text(_SESSION_SQL), {"h": token_digest(token), "now": now})).first()
            if row is None:
                return None
            if now - row.last_seen_at > timedelta(minutes=1):   # 매 요청마다 쓰지 않게 1분에 한 번만
                await s.execute(text("UPDATE login_session SET last_seen_at = :n WHERE token_hash = :h"),
                                {"n": now, "h": token_digest(token)})
                await s.commit()
        return principal_from_row(row)

    async def logout(self, token: str) -> None:
        if not token:
            return
        async with self._sessions() as s:
            row = (await s.execute(text("""
                UPDATE login_session SET revoked_at = :n WHERE token_hash = :h AND revoked_at IS NULL
                RETURNING user_id"""), {"n": self._now(), "h": token_digest(token)})).first()
            if row is not None:
                user = (await s.execute(text(_USER_SQL + " WHERE u.id = :i GROUP BY u.id"), {"i": row.user_id})).first()
                if user is not None:
                    await self._audit(s, "logout", user, None)
            await s.commit()

    async def revoke_all(self, login_id: str) -> int:
        """관리자용: 그 사람의 모든 세션을 끊는다 (비밀번호 바꾼 뒤·퇴사). 끊은 개수."""
        async with self._sessions() as s:
            result = await s.execute(text("""
                UPDATE login_session SET revoked_at = :n
                WHERE revoked_at IS NULL AND user_id = (SELECT id FROM users WHERE login_id = :l)"""),
                {"n": self._now(), "l": login_id})
            await s.commit()
            return result.rowcount or 0

    @staticmethod
    async def _audit(s: AsyncSession, event: str, row: Any, reason: str | None) -> None:
        snapshot = {"user_id": row.login_id, "roles": list(row.roles or ()), "dept_id": row.dept_id,
                    "security_grade": row.security_grade}
        await s.execute(text("""
            INSERT INTO audit_log (event_type, user_id, principal_snapshot, reason, executed)
            VALUES (:e, :u, CAST(:p AS JSONB), :r, :x)"""),
            {"e": event, "u": row.id, "p": json.dumps(snapshot, ensure_ascii=False), "r": reason,
             "x": event == "login"})
