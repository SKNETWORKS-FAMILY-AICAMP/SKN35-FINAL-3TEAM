"""증분 동기화 → 관계형 DB · 삭제 감지 · 실패 사유 · 중단 후 재개."""

import pytest

from gmail_mcp.fake import KIM_ID, PARK, make_message, ts
from gmail_mcp.models import DeletedCandidate, FailureReason, MailCheckpoint, SyncFailure
from gmail_mcp.runner import PruneGuardError, run_incremental, run_prune
from gmail_mcp.sync import GmailSyncConnector, build_time_range_query


def conn(box, **kw):
    return GmailSyncConnector(box, KIM_ID, **kw)


def test_time_range_query():
    assert build_time_range_query(1700000000.9, 1710000000) == "after:1700000000 before:1710000000"
    assert build_time_range_query(None, None) is None


def test_overlap_moves_start_back():
    assert conn(None, overlap_seconds=600).window_query(1_000_000, 2_000_000) == "after:999400 before:2000000"


async def test_incremental_stores_into_db_then_rerun_writes_nothing(kim_box, store):
    first = await run_incremental(conn(kim_box), store, now=ts(20).timestamp(), full=True)
    assert first.changes == {"new": 3} and first.completed
    assert await store.message_ids(KIM_ID) == {"m1", "m2", "m3"}
    again = await run_incremental(conn(kim_box), store, now=ts(21).timestamp(), full=True)
    assert again.changes == {} and again.unchanged == 3


async def test_next_window_only_new_mail(kim_box, store):
    await run_incremental(conn(kim_box, overlap_seconds=0), store, now=ts(16).timestamp())
    kim_box.add(make_message("m4", thread_id="t4", sender=PARK, to=["sky.kim@acme.example"], subject="신규",
                             body="새 메일", sent_at=ts(17)))
    second = await run_incremental(conn(kim_box, overlap_seconds=0), store, now=ts(18).timestamp())
    assert second.changes == {"new": 1} and kim_box.list_calls[-1][0].startswith("after:")
    assert (await store.get_sync_state(KIM_ID)).last_sync_end == ts(18).timestamp()


async def test_failure_reasons_recorded_in_db(kim_box, store):
    kim_box.fail_ids.add("m2")
    kim_box.messages["m3"]["sizeEstimate"] = 10**9
    stats = await run_incremental(conn(kim_box), store, now=ts(20).timestamp())
    assert stats.failures == {"fetch_error": 1, "too_large": 1}
    rows = await store.failures(KIM_ID, stats.job_id)
    assert {r["message_id"]: r["reason"] for r in rows} == {"m2": "fetch_error", "m3": "too_large"}
    assert (await store.runs(KIM_ID))[-1]["failures"] == {"fetch_error": 1, "too_large": 1}


async def test_vanished_message_is_deleted_not_failure(kim_box, store):
    await run_incremental(conn(kim_box), store, now=ts(20).timestamp())
    kim_box.vanish_ids.add("m1")
    results, _ = await conn(kim_box).load_from_checkpoint(None, None, MailCheckpoint())
    assert DeletedCandidate(message_id="m1") in results and not any(isinstance(r, SyncFailure) for r in results)
    stats = await run_incremental(conn(kim_box), store, now=ts(21).timestamp(), full=True)
    assert stats.changes == {"deleted": 1} and "m1" not in await store.message_ids(KIM_ID)


async def test_resume_after_crash(kim_box, store):
    c = conn(kim_box, page_size=1)
    original = c.load_from_checkpoint
    calls = {"n": 0}

    async def crash_on_second_page(start, end, cp):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("네트워크 끊김")
        return await original(start, end, cp)

    c.load_from_checkpoint = crash_on_second_page
    with pytest.raises(ConnectionError):
        await run_incremental(c, store, now=ts(20).timestamp())
    state = await store.get_sync_state(KIM_ID)
    assert state.in_progress and state.page_token == "1" and state.last_sync_end is None

    c.load_from_checkpoint = original
    resumed = await run_incremental(c, store)
    assert resumed.resumed and resumed.changes == {"new": 2}
    assert await store.message_ids(KIM_ID) == {"m1", "m2", "m3"}
    assert not (await store.get_sync_state(KIM_ID)).in_progress


async def test_max_pages_stops_and_next_run_continues(kim_box, store):
    first = await run_incremental(conn(kim_box, page_size=1), store, now=ts(20).timestamp(), max_pages=1)
    assert not first.completed and first.changes == {"new": 1}
    rest = await run_incremental(conn(kim_box, page_size=1), store)
    assert rest.resumed and rest.completed and rest.changes == {"new": 2}


async def test_prune_deletes_missing(kim_box, store):
    await run_incremental(conn(kim_box), store, now=ts(20).timestamp())
    kim_box.remove("m1")
    stats = await run_prune(conn(kim_box), store, min_docs=100)
    assert stats.changes == {"deleted": 1} and await store.message_ids(KIM_ID) == {"m2", "m3"}


async def test_prune_guard_stops_mass_deletion(kim_box, store):
    await run_incremental(conn(kim_box), store, now=ts(20).timestamp())
    kim_box.remove("m1")
    kim_box.remove("m2")  # 3통 중 2통(67%) 사라짐 — 계정 착오 등 의심
    with pytest.raises(PruneGuardError, match="반영하지 않았습니다"):
        await run_prune(conn(kim_box), store, max_ratio=0.3, min_docs=3)
    assert await store.message_ids(KIM_ID) == {"m1", "m2", "m3"}
    assert (await run_prune(conn(kim_box), store, max_ratio=0.3, min_docs=3, force=True)).changes == {"deleted": 2}


async def test_message_list_aborts_on_repeated_page_token(kim_box):
    async def looping(query, token, max_results=100):
        return ["m1"], "same-token"
    kim_box.list_messages = looping
    with pytest.raises(RuntimeError, match="토큰 반복"):
        await conn(kim_box).list_all_message_ids()


def test_failure_reason_values():
    assert {r.value for r in FailureReason} == {"fetch_error", "too_large", "invalid_message"}
