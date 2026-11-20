"""관계형 DB 저장소 — 주인 조건 강제 · 변경 판정 · 검색."""

import pytest

from gmail_mcp.convert import message_from_api, to_record
from gmail_mcp.fake import CHOI, KIM, PARK, make_message, ts
from gmail_mcp.models import ChangeKind


def rec(owner, mid, *, thread="t1", subject="경비 정산", body="본문", day=1, labels=None):
    msg = make_message(mid, thread_id=thread, sender=PARK, to=[CHOI], subject=subject, body=body, sent_at=ts(day),
                       labels=labels)
    return to_record(message_from_api(msg), owner, 100)


async def test_upsert_new_unchanged_updated(store):
    assert await store.upsert(rec("E1", "a")) == ChangeKind.NEW
    assert await store.upsert(rec("E1", "a")) is None  # 같으면 쓰지 않음
    assert await store.upsert(rec("E1", "a", labels=["INBOX", "STARRED"])) == ChangeKind.UPDATED


async def test_owner_isolation(store):
    await store.upsert(rec("E1", "a", thread="shared", subject="연봉"))
    await store.upsert(rec("E2", "b", thread="shared", subject="회의"))
    assert [m.message_id for m in await store.thread("E1", "shared")] == ["a"]  # 같은 thread_id 라도 주인별
    assert [m.message_id for m in await store.search("E2", "", 10)] == ["b"]
    assert await store.search("E2", "연봉", 10) == []
    assert await store.message_ids("E1") == {"a"}


async def test_every_query_requires_owner(store):
    for call in (store.search("", "x", 1), store.thread(" ", "t"), store.message_ids(""), store.delete("", ["a"])):
        with pytest.raises(ValueError, match="owner_user_id"):
            await call


async def test_search_words_and_like_escape(store):
    await store.upsert(rec("E1", "a", subject="11월 경비 정산", day=1))
    await store.upsert(rec("E1", "b", subject="경비 100% 환급", day=2))
    await store.upsert(rec("E1", "c", subject="인사평가", body="일정 안내", day=3))
    assert [m.message_id for m in await store.search("E1", "경비", 10)] == ["b", "a"]  # 최신순
    assert [m.message_id for m in await store.search("E1", "경비 정산", 10)] == ["a"]  # 모든 단어 포함
    assert [m.message_id for m in await store.search("E1", "100%", 10)] == ["b"]  # % 는 글자 그대로
    assert [m.message_id for m in await store.search("E1", "%", 10)] == ["b"]
    assert [m.message_id for m in await store.search("E1", "안내", 10)] == ["c"]  # 본문 검색
    assert all(m.body == "" for m in await store.search("E1", "", 10))  # 목록에는 본문을 싣지 않음


async def test_delete_only_own(store):
    await store.upsert(rec("E1", "a"))
    await store.upsert(rec("E2", "a"))
    assert await store.delete("E1", ["a", "zzz"]) == 1
    assert await store.message_ids("E2") == {"a"}


async def test_account_mapping(store):
    await store.set_account("E1001", KIM.upper())
    assert await store.get_account_email("E1001") == KIM
    assert await store.connected_users() == ["E1001"]
