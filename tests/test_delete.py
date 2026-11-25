"""대화 삭제 (DELETE /api/chat/sessions/{id}) 와 내 문서 삭제 (DELETE /api/documents/{id}). 본인 것만 지운다."""

from __future__ import annotations

from services.documents import DocumentStore, new_document_id
from tests.test_api import KIM, make

PARK = {"X-User-Id": "park"}


async def test_delete_own_session_only(tmp_path):
    client, svc = make([{"content": "안녕하세요"}], tmp_path)
    sid = (await client.post("/api/chat/messages", json={"content": "hi"}, headers=KIM)).json()["session_id"]
    assert [s["session_id"] for s in (await client.get("/api/chat/sessions", headers=KIM)).json()] == [sid]

    assert (await client.delete(f"/api/chat/sessions/{sid}", headers=PARK)).status_code == 404   # 남의 대화
    assert (await client.delete("/api/chat/sessions/nope", headers=KIM)).status_code == 404      # 없는 대화
    assert (await client.delete(f"/api/chat/sessions/{sid}", headers=KIM)).status_code == 204
    assert (await client.get("/api/chat/sessions", headers=KIM)).json() == []
    assert (await client.get(f"/api/chat/sessions/{sid}/turns", headers=KIM)).status_code == 404


async def test_session_answering_now_is_not_deleted(tmp_path):
    client, svc = make([{"content": "안녕하세요"}], tmp_path)
    sid = (await client.post("/api/chat/messages", json={"content": "hi"}, headers=KIM)).json()["session_id"]
    session = await svc.sessions.get_owned(sid, "kim")
    async with session.lock:   # 답을 만드는 중
        assert (await client.delete(f"/api/chat/sessions/{sid}", headers=KIM)).status_code == 409
    assert (await client.delete(f"/api/chat/sessions/{sid}", headers=KIM)).status_code == 204


async def test_delete_own_document_only(tmp_path):
    client, svc = make([], tmp_path)
    svc.documents = DocumentStore(tmp_path / "docs.sqlite3")
    document_id = new_document_id()
    await svc.documents.put({"document_id": document_id, "owner_id": "kim", "kind": "form",
                             "document": {"title": "정리표", "blocks": []}})
    assert [d["document_id"] for d in (await client.get("/api/documents", headers=KIM)).json()] == [document_id]

    assert (await client.delete(f"/api/documents/{document_id}", headers=PARK)).status_code == 404
    assert (await client.delete(f"/api/documents/{document_id}", headers=KIM)).status_code == 204
    assert (await client.get("/api/documents", headers=KIM)).json() == []
    assert (await client.get(f"/api/documents/{document_id}", headers=KIM)).status_code == 404
    assert (await client.delete(f"/api/documents/{document_id}", headers=KIM)).status_code == 404
