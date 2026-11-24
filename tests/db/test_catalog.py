"""사내 문서 목록(knowledge/catalog.py)을 실제 DB 에서 확인. 트랜잭션 안에서 하고 되돌린다.

원칙: 직원은 에이전트가 찾아올 수 있는 문서(전사 공개 + 내 부서)만, 관리자는 목록 전체(searchable 로 구분)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.catalog import list_docs
from tests.db.test_retrieval import _doc
from tests.db.test_schema import _user, conn  # noqa: F401


async def _ready(conn, version_id, title, chunks=2):
    await conn.execute(text("UPDATE doc_version SET status = 'ready' WHERE id = :v"), {"v": version_id})
    await conn.execute(text("UPDATE doc SET title = :t WHERE id = (SELECT doc_id FROM doc_version WHERE id = :v)"),
                       {"t": title, "v": version_id})
    for i in range(chunks):
        await conn.execute(text(
            "INSERT INTO chunk (doc_version_id, chunk_index, content, owner_dept_id, scope, security_grade) "
            "SELECT v.id, :i, '본문', d.owner_dept_id, d.scope, d.security_grade "
            "FROM doc_version v JOIN doc d ON d.id = v.doc_id WHERE v.id = :v"), {"i": i, "v": version_id})


async def test_list_matches_what_the_agent_can_search(conn):  # noqa: F811
    uid = await _user(conn)
    dept = await conn.scalar(text("SELECT dept_id FROM users WHERE id = :u"), {"u": uid})
    other = await conn.scalar(text("INSERT INTO dept (name) VALUES ('시험 다른 부서') RETURNING id"))
    shown = await _doc(conn, uid, dept, "company", 1)
    await _ready(conn, shown, "시험 전사 규정", chunks=3)
    secret = await _doc(conn, uid, dept, "company", 5)          # 등급 5 → 등급 2 사용자에게 안 보임
    await _ready(conn, secret, "시험 비밀 규정")
    mine = await _doc(conn, uid, dept, "dept", 1)               # 내 부서 전용 → 보임 (검색에도 나옴)
    await _ready(conn, mine, "시험 부서 규정")
    theirs = await _doc(conn, uid, other, "dept", 1)            # 다른 부서 전용 → 안 보임 (관리자 목록에는 보임)
    await _ready(conn, theirs, "시험 남의 부서 규정")
    old = await _doc(conn, uid, dept, "company", 1, current=False)   # 옛 버전 → 안 보임
    await _ready(conn, old, "시험 옛 규정")
    busy = await _doc(conn, uid, dept, "company", 1)            # 처리 중 → 안 보임
    await conn.execute(text("UPDATE doc SET title = '시험 처리중 규정' WHERE id = (SELECT doc_id FROM doc_version WHERE id = :v)"),
                       {"v": busy})

    async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
        staff = await list_docs(session, dept_id=dept, security_grade=2)
        admin = await list_docs(session, dept_id=dept, security_grade=2, admin=True)
        no_dept = await list_docs(session, dept_id=None, security_grade=2)
    titles = {d["title"] for d in staff}
    assert {"시험 전사 규정", "시험 부서 규정"} <= titles
    assert not titles & {"시험 비밀 규정", "시험 남의 부서 규정", "시험 옛 규정", "시험 처리중 규정"}
    assert next(d for d in staff if d["title"] == "시험 전사 규정")["chunks"] == 3
    assert all(d["searchable"] for d in staff)
    assert next(d for d in staff if d["title"] == "시험 부서 규정")["scope"] == "dept"

    by_title = {d["title"]: d for d in admin}
    assert "시험 남의 부서 규정" in by_title and by_title["시험 남의 부서 규정"]["searchable"] is False
    assert by_title["시험 남의 부서 규정"]["dept_name"] == "시험 다른 부서"
    assert not set(by_title) & {"시험 비밀 규정", "시험 옛 규정", "시험 처리중 규정"}   # 등급·버전·상태 조건은 관리자도 같음

    assert "시험 부서 규정" not in {d["title"] for d in no_dept}   # 부서가 없으면 전사 공개만
