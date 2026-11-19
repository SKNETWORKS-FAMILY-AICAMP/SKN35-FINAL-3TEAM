"""증분 동기화 · 권한 동기화 · 삭제 감지 · 실행기(중단 후 재개)."""

import json

import pytest
from gmail_mcp.fake import CHOI, KIM, PARK, ts

from gmail_mcp.fake import TEAM_ACCOUNT, make_message
from gmail_mcp.models import MailCheckpoint, MailDocument, SyncFailure
from gmail_mcp.perm_sync import find_deleted_docs, gmail_doc_sync
from gmail_mcp.runner import load_state, run_incremental
from gmail_mcp.sync import GmailSyncConnector, build_time_range_query


def test_time_range_query():
    assert build_time_range_query(1700000000.9, 1710000000) == "after:1700000000 before:1710000000"
    assert build_time_range_query(None, 1710000000) == "before:1710000000"
    assert build_time_range_query(None, None) is None


def test_overlap_moves_start_back():
    c = GmailSyncConnector(None, TEAM_ACCOUNT, overlap_seconds=600)
    assert c.window_query(1_000_000, 2_000_000) == "after:999400 before:2000000"
    assert c.window_query(None, 2_000_000) == "before:2000000"


async def drain(connector, start, end):
    cp, pages, out = MailCheckpoint(), 0, []
    while cp.has_more:
        results, cp = await connector.load_from_checkpoint(start, end, cp)
        out += results
        pages += 1
    return out, pages


async def test_paginates_with_checkpoint(mailbox):
    out, pages = await drain(GmailSyncConnector(mailbox, TEAM_ACCOUNT, page_size=2), None, None)
    assert pages == 2
    assert sorted(d.thread_id for d in out) == ["t1", "t2", "t3"]
    assert mailbox.list_calls[1][1] == "2"  # 두 번째 호출은 저장된 페이지 토큰으로


async def test_incremental_window_only_new_threads(mailbox):
    out, _ = await drain(GmailSyncConnector(mailbox, TEAM_ACCOUNT, overlap_seconds=0),
                         ts(11).timestamp(), ts(20).timestamp())
    assert sorted(d.thread_id for d in out) == ["t2", "t3"]


async def test_new_reply_brings_whole_thread_back(mailbox):
    mailbox.add_thread("t1", make_message("m1b", sender=KIM, to=[CHOI], subject="Re: 인사평가 일정",
                                          body="참고 부탁", sent_at=ts(19)))
    out, _ = await drain(GmailSyncConnector(mailbox, TEAM_ACCOUNT, overlap_seconds=0),
                         ts(18).timestamp(), ts(20).timestamp())
    [doc] = out
    assert doc.thread_id == "t1" and len(doc.messages) == 2
    assert doc.access.allows(CHOI)  # 새 참여자가 권한에 추가됨


async def test_one_failure_does_not_stop_sync(mailbox):
    mailbox.fail_threads.add("t2")
    out, _ = await drain(GmailSyncConnector(mailbox, TEAM_ACCOUNT), None, None)
    failures = [r for r in out if isinstance(r, SyncFailure)]
    assert [f.doc_id for f in failures] == ["gmail:t2"]
    assert len([r for r in out if isinstance(r, MailDocument)]) == 2


async def test_perm_sync_emits_access_for_every_doc(mailbox):
    updates = [u async for u in gmail_doc_sync(GmailSyncConnector(mailbox, TEAM_ACCOUNT, page_size=1))]
    by_id = {u.doc_id: u.access for u in updates}
    assert set(by_id) == {"gmail:t1", "gmail:t2", "gmail:t3"}
    assert by_id["gmail:t2"].allows(KIM) and not by_id["gmail:t3"].allows(KIM)


async def test_perm_sync_stop_signal(mailbox):
    with pytest.raises(RuntimeError, match="중단"):
        async for _ in gmail_doc_sync(GmailSyncConnector(mailbox, TEAM_ACCOUNT), should_stop=lambda: True):
            pass


async def test_slim_listing_aborts_on_repeated_page_token(mailbox):
    async def looping(query, token, max_results=100):
        return ["t1"], "same-token"
    mailbox.list_threads = looping
    with pytest.raises(RuntimeError, match="토큰 반복"):
        async for _ in GmailSyncConnector(mailbox, TEAM_ACCOUNT).retrieve_all_slim_docs_perm_sync():
            pass


async def test_find_deleted_docs(mailbox):
    deleted = await find_deleted_docs(GmailSyncConnector(mailbox, TEAM_ACCOUNT),
                                      ["gmail:t1", "gmail:t2", "gmail:gone", "upload:doc-1"])
    assert deleted == {"gmail:gone"}  # 다른 소스 문서는 건드리지 않음


async def test_runner_incremental_then_next_window(mailbox, tmp_path):
    conn = GmailSyncConnector(mailbox, TEAM_ACCOUNT, overlap_seconds=0)
    first = await run_incremental(conn, tmp_path, now=ts(16).timestamp())
    assert first.documents == 3
    assert load_state(tmp_path / "state.json").last_sync_end == ts(16).timestamp()

    mailbox.add_thread("t4", make_message("m4", sender=PARK, to=[KIM], subject="신규", body="새 메일", sent_at=ts(17)))
    second = await run_incremental(conn, tmp_path, now=ts(18).timestamp())
    assert second.documents == 1
    lines = (tmp_path / "documents" / f"{second.job_id}.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["thread_id"] == "t4"


async def test_runner_resumes_from_checkpoint_after_crash(mailbox, tmp_path):
    conn = GmailSyncConnector(mailbox, TEAM_ACCOUNT, page_size=1)
    original = conn.load_from_checkpoint
    calls = {"n": 0}

    async def crash_on_second_page(start, end, cp):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("네트워크 끊김")
        return await original(start, end, cp)

    conn.load_from_checkpoint = crash_on_second_page
    with pytest.raises(ConnectionError):
        await run_incremental(conn, tmp_path, now=ts(20).timestamp())
    saved = load_state(tmp_path / "state.json")
    assert saved.checkpoint.page_token == "1" and saved.last_sync_end is None

    conn.load_from_checkpoint = original
    resumed = await run_incremental(conn, tmp_path)
    assert resumed.resumed and resumed.documents == 2  # 1페이지는 이미 처리됨 → 나머지 2건만
    assert load_state(tmp_path / "state.json").last_sync_end == ts(20).timestamp()
