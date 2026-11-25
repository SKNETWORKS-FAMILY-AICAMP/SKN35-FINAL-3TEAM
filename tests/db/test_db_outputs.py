"""알림함·문서·고객 안내 발송 기록의 DB 저장소 (STORE=db, 마이그레이션 0013). DB 가 없으면 건너뜀.

tests/db/test_db_stores.py 의 db 픽스처를 쓴다 (바깥 트랜잭션 안에서 하고 끝에 되돌림).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from services.customer_notices import DbNoticeLog
from services.documents import DbDocumentStore, new_document_id
from services.inbox import DbInboxStore
from storage import LocalStorage
from tests.db.test_db_stores import db  # noqa: F401


@pytest.fixture
def need_0013(db):  # noqa: F811
    async def check():
        if not await db["conn"].scalar(text("SELECT to_regclass('notification') IS NOT NULL")):
            pytest.skip("마이그레이션 0013 전 DB")
    return check


async def test_inbox_is_private_and_tracks_read_and_delete(db, need_0013):  # noqa: F811
    await need_0013()
    kim, park = db["p"]["kim"].user_id, db["p"]["park"].user_id
    inbox = DbInboxStore(db["sessions"])
    alert = await inbox.add(kim, "maturity_alert", "만기 대출 6건", "오늘 먼저 보세요.", False,
                            {"key": "2026-11-23:30", "counts": {"7일 이내": 2}}, has_customer_info=True,
                            session_id="not-a-uuid")
    assert alert["alert_id"].isdigit() and alert["read_at"] is None
    assert (await inbox.find(kim, "maturity_alert", "2026-11-23:30"))["alert_id"] == alert["alert_id"]
    assert await inbox.find(kim, "maturity_alert", "2026-11-23:7") is None
    assert [a["alert_id"] for a in await inbox.list(kim)] == [alert["alert_id"]] and await inbox.list(park) == []
    assert await inbox.unread_count(kim) == 1
    assert await inbox.open(park, alert["alert_id"]) is None and not await inbox.delete(park, alert["alert_id"])
    opened = await inbox.open(kim, alert["alert_id"])
    assert opened["read_at"] and opened["payload"]["counts"]["7일 이내"] == 2 and await inbox.unread_count(kim) == 0
    assert await inbox.delete(kim, alert["alert_id"]) and await inbox.list(kim) == []
    with pytest.raises(ValueError):
        await inbox.add(kim, "unknown", "x", "", False, {})


async def test_documents_live_in_output_file_and_downloads_are_audited(db, need_0013, tmp_path):  # noqa: F811
    await need_0013()
    kim, park = db["p"]["kim"].user_id, db["p"]["park"].user_id
    store = DbDocumentStore(db["sessions"], LocalStorage(tmp_path))
    document = {"schema_version": 1, "title": "만기 대출 정리표", "blocks": [{"type": "paragraph", "text": "본문"}]}
    record = await store.save(kim, document, form_id="maturity-report", has_customer_info=True)
    doc_id = record["document_id"]
    assert (await store.get(kim, doc_id))["document"] == document and await store.get(park, doc_id) is None
    assert [d["document_id"] for d in await store.list(kim)] == [doc_id] and await store.list(park) == []
    row = (await db["conn"].execute(text("SELECT kind, has_customer_info, storage_path, mime_type FROM output_file "
                                         "WHERE public_id = :p"), {"p": doc_id})).first()
    assert row.kind == "form" and row.has_customer_info and row.storage_path.startswith(f"outputs/{kim}/")
    await store.log_download(kim, doc_id, "pdf")
    await store.log_download(park, doc_id, "pdf")  # 남의 문서는 기록도 하지 않는다
    audited = (await db["conn"].execute(text("SELECT a.arguments FROM audit_log a JOIN output_file o ON o.id = a.target_id "
                                             "WHERE a.event_type = 'output_download' AND o.public_id = :p"),
                                        {"p": doc_id})).all()
    assert [a.arguments["format"] for a in audited] == ["pdf"]
    # 파일이 바뀌면 열지 않는다
    (tmp_path / row.storage_path).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="바뀌어"):
        await store.get(kim, doc_id)
    # 작업 기록(시장 조회 결과)은 본인 것만
    ident = new_document_id()
    await store.put_record("snapshots", ident, kim, {"id": ident, "fx": []})
    assert (await store.get_record("snapshots", ident, kim))["fx"] == [] and await store.get_record("snapshots", ident, park) is None


async def test_notice_once_per_loan_and_maturity_date(db, need_0013):  # noqa: F811
    await need_0013()
    kim = db["p"]["kim"].user_id
    log = DbNoticeLog(db["sessions"])
    args = (kim, "L-T001", "2026-11-26", "a@example.com", "[대출 만기 안내] 만기일 안내")
    notice_id = await log.reserve(*args, "c1")
    assert notice_id and await log.reserve(*args, "c2") is None  # 두 번 눌러도 한 건만 (부분 유일 인덱스)
    assert (await log.active("L-T001", "2026-11-26"))["status"] == "sending"
    await log.finish(notice_id, "sent", message_id="m-1")
    sent = await log.active("L-T001", "2026-11-26")
    assert sent["status"] == "sent" and sent["message_id"] == "m-1" and sent["sent_at"]
    assert await log.reserve(*args, "c3") is None
    # 만기일이 바뀌면(연장) 새로 보낼 수 있다. 실패·반려는 다시 보낼 수 있다
    other = await log.reserve(kim, "L-T001", "2027-05-26", "a@example.com", "제목", "c4")
    await log.finish(other, "failed", error="거부")
    assert await log.active("L-T001", "2027-05-26") is None
    await log.reject(kim, "L-T002", "2026-11-30", "b@example.com", "제목", "c5", "반려")
    assert await log.active("L-T002", "2026-11-30") is None
    assert [h["status"] for h in await log.history(kim)] == ["rejected", "failed", "sent"]
