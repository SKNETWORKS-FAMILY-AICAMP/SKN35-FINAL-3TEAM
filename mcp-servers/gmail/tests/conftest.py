import os
import uuid
from contextlib import asynccontextmanager

import pytest

from gmail_mcp.config import Settings
from gmail_mcp.fake import CHOI, CHOI_ID, KIM, KIM_ID, LEE, PARK, FakeGmailClient, make_message, ts
from gmail_mcp.google_auth import AccountNotConnected
from gmail_mcp.service import MailService
from gmail_mcp.store import MailStore


# PostgreSQL 에서도 같은 테스트를 돌린다. 쓰고 지워도 되는 DB 를 GMAIL_TEST_DATABASE_URL 로 직접 지정할 때만 실행
# (alembic upgrade head 까지 마친 DB). 예: postgresql://postgres@127.0.0.1:5432/agent_platform_test
PG_LOGINS = ("E1", "E2", "E1001", "E1002", "E2001", "E2002")  # 테스트가 owner 로 쓰는 로그인 ID → users 에 미리 만든다


def _pg_url() -> str:
    url = os.environ.get("GMAIL_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("GMAIL_TEST_DATABASE_URL 없음 — PostgreSQL 저장소 테스트 건너뜀")
    return url


@asynccontextmanager
async def open_pg_store(url: str):
    import asyncpg

    from gmail_mcp.store_pg import PostgresMailStore, to_asyncpg_dsn

    admin = await asyncpg.connect(to_asyncpg_dsn(url))
    dept = await admin.fetchval("INSERT INTO department (name) VALUES ($1) RETURNING id",
                                f"mail-test-{uuid.uuid4().hex[:8]}")
    for login in PG_LOGINS:
        await admin.execute("DELETE FROM users WHERE login_id = $1", login)  # 이전 실행 찌꺼기
        await admin.execute("DELETE FROM mail_archive WHERE former_login_id = $1", login)  # (삭제하면 트리거가 보관함)
        await admin.execute("INSERT INTO users (login_id, name, dept_id) VALUES ($1, $2, $3)", login, f"test-{login}",
                            dept)
    store = PostgresMailStore(url)
    try:
        yield store
    finally:
        await store.close()
        await admin.execute("DELETE FROM users WHERE login_id = ANY($1::text[])", list(PG_LOGINS))  # 메일은 보관함으로
        await admin.execute("DELETE FROM mail_archive WHERE former_login_id = ANY($1::text[])", list(PG_LOGINS))
        await admin.execute("DELETE FROM department WHERE id = $1", dept)
        await admin.close()


@pytest.fixture
async def pg_store():
    async with open_pg_store(_pg_url()) as store:
        yield store


@pytest.fixture(params=["sqlite", "postgres"])
async def store(request, tmp_path):
    """같은 저장소 테스트를 SQLite · PostgreSQL 양쪽에 돌린다."""
    if request.param == "sqlite":
        yield MailStore(tmp_path / "mail.sqlite3")
    else:
        async with open_pg_store(_pg_url()) as pg:
            yield pg


@pytest.fixture
def kim_box() -> FakeGmailClient:
    return FakeGmailClient(KIM).add(
        make_message("m1", thread_id="t1", sender=KIM, to=[LEE], subject="인사평가 일정", body="12월 8일 시작",
                     sent_at=ts(10), labels=["SENT"]),
        make_message("m2", thread_id="t2", sender=PARK, to=[CHOI], cc=[KIM], subject="경비 정산 마감",
                     body="11월 28일까지", sent_at=ts(12)),
        make_message("m3", thread_id="t2", sender=KIM, to=[PARK], bcc=[LEE], subject="Re: 경비 정산 마감",
                     body="<p>확인했습니다</p>", sent_at=ts(15), html=True, labels=["SENT"]),
    )


@pytest.fixture
def choi_box() -> FakeGmailClient:
    return FakeGmailClient(CHOI).add(
        make_message("c1", thread_id="ct1", sender=PARK, to=[CHOI], subject="연봉 상한 확정",
                     body="올해 연봉 상한은 5% 입니다", sent_at=ts(11)),
    )


@pytest.fixture
def boxes(kim_box, choi_box) -> dict[str, FakeGmailClient]:
    return {KIM_ID: kim_box, CHOI_ID: choi_box}


@pytest.fixture
def clients(boxes):
    def factory(user_id: str):
        if user_id not in boxes:
            raise AccountNotConnected(user_id)
        return boxes[user_id]
    return factory


@pytest.fixture
def svc(store, clients) -> MailService:
    return MailService(store, clients, Settings(adapter="fake"))
