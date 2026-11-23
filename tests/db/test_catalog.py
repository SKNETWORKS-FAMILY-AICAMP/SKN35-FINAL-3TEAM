"""사내 문서 목록(knowledge/catalog.py)을 실제 DB 에서 확인. 트랜잭션 안에서 하고 되돌린다."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.catalog import list_company_docs
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


async def test_only_company_docs_the_user_may_read(conn):  # noqa: F811
    uid = await _user(conn)
    dept = await conn.scalar(text("SELECT dept_id FROM users WHERE id = :u"), {"u": uid})
    shown = await _doc(conn, uid, dept, "company", 1)
    await _ready(conn, shown, "시험 전사 규정", chunks=3)
    secret = await _doc(conn, uid, dept, "company", 5)          # 등급 5 → 등급 2 사용자에게 안 보임
    await _ready(conn, secret, "시험 비밀 규정")
    mine = await _doc(conn, uid, dept, "dept", 1)               # 부서 전용 → 1단계 목록에는 안 넣음
    await _ready(conn, mine, "시험 부서 규정")
    old = await _doc(conn, uid, dept, "company", 1, current=False)   # 옛 버전 → 안 보임
    await _ready(conn, old, "시험 옛 규정")
    busy = await _doc(conn, uid, dept, "company", 1)            # 처리 중 → 안 보임
    await conn.execute(text("UPDATE doc SET title = '시험 처리중 규정' WHERE id = (SELECT doc_id FROM doc_version WHERE id = :v)"),
                       {"v": busy})

    async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
        docs = await list_company_docs(session, security_grade=2)
    titles = {d["title"] for d in docs}
    assert "시험 전사 규정" in titles
    assert not titles & {"시험 비밀 규정", "시험 부서 규정", "시험 옛 규정", "시험 처리중 규정"}
    assert next(d for d in docs if d["title"] == "시험 전사 규정")["chunks"] == 3
