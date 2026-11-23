"""메일 툴 — 본인 메일만 · DB 조회 · 본인 계정 발송."""

import base64
import email
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from gmail_mcp.config import Settings
from gmail_mcp.fake import CHOI, CHOI_ID, KIM_ID, LEE, LEE_ID, PARK, make_message
from gmail_mcp.server import build_server
from gmail_mcp.service import NOT_CONNECTED, NOT_FOUND, InvalidRequest, MailService
from gmail_mcp.store import MailStore


# ── 검색 · 읽기: 본인 메일만, DB 에서 ──

async def test_search_syncs_first_then_reads_db(svc, store):
    res = await svc.search_mail(KIM_ID)
    assert res.synced and [t.thread_id for t in res.threads] == ["t2", "t1"]  # 최신순
    assert next(t for t in res.threads if t.thread_id == "t2").message_count == 2
    assert await store.message_ids(KIM_ID) == {"m1", "m2", "m3"}  # 관계형 DB 에 저장됨


async def test_search_words(svc):
    assert [t.thread_id for t in (await svc.search_mail(KIM_ID, "경비")).threads] == ["t2"]
    assert (await svc.search_mail(KIM_ID, "없는단어")).threads == []


async def test_cannot_read_other_employees_mail_even_with_thread_id(svc):
    await svc.search_mail(CHOI_ID)  # 최민준 메일함 동기화 (연봉 메일 ct1)
    with pytest.raises(InvalidRequest, match=NOT_FOUND):
        await svc.read_mail(KIM_ID, "ct1")  # 김하늘이 thread_id 를 알아도 못 엶
    assert [t.thread_id for t in (await svc.search_mail(KIM_ID, "연봉")).threads] == []


async def test_read_own_thread_with_bcc_of_own_sent_mail(svc):
    await svc.search_mail(KIM_ID)
    thread = await svc.read_mail(KIM_ID, "t2")
    assert [m.message_id for m in thread.messages] == ["m2", "m3"]
    assert thread.messages[1].bcc == [LEE]  # 본인이 보낸 메일의 숨은참조는 본인에게 보임
    assert thread.messages[1].body == "확인했습니다"


async def test_new_mail_visible_without_manual_sync(svc, kim_box):
    await svc.search_mail(KIM_ID)
    just_now = datetime.now(timezone.utc) - timedelta(seconds=1)  # 방금 도착한 메일
    kim_box.add(make_message("m9", thread_id="t9", sender=PARK, to=["sky.kim@acme.example"], subject="긴급 공지",
                             body="오늘 오후 점검", sent_at=just_now))
    assert [t.thread_id for t in (await svc.search_mail(KIM_ID, "긴급")).threads] == ["t9"]


async def test_search_still_works_when_gmail_unreachable(svc, kim_box):
    await svc.search_mail(KIM_ID)

    async def down(*a, **k):
        raise ConnectionError("Gmail 연결 실패")
    kim_box.list_messages = down
    res = await svc.search_mail(KIM_ID, "경비")
    assert not res.synced and [t.thread_id for t in res.threads] == ["t2"]  # 저장된 메일로 검색


async def test_not_connected_account(svc):
    with pytest.raises(InvalidRequest, match=NOT_CONNECTED):
        await svc.search_mail(LEE_ID)
    with pytest.raises(InvalidRequest, match=NOT_CONNECTED):
        await svc.send_mail(LEE_ID, [PARK], "제목", "본문")


async def test_read_mail_truncates_long_body(store, clients):
    svc = MailService(store, clients, Settings(adapter="fake", max_read_body_chars=3))
    await svc.search_mail(KIM_ID)
    thread = await svc.read_mail(KIM_ID, "t1")
    assert thread.truncated and thread.messages[0].body.startswith("12월")


@pytest.mark.parametrize("requester", ["", "../etc", "a b"])
async def test_requester_required(svc, requester):
    with pytest.raises(InvalidRequest, match="요청자"):
        await svc.search_mail(requester)


# ── 발송: 본인 계정으로 ──

def sent_message(box):
    return email.message_from_bytes(base64.urlsafe_b64decode(box.sent[-1]))


async def test_send_mail_uses_requesters_own_account(svc, kim_box, choi_box):
    res = await svc.send_mail(KIM_ID, [" Doyun.Lee@acme.example "], "회의 일정", "내일 10시", cc=[PARK])
    assert res.to == [LEE] and res.cc == [PARK] and res.status == "simulated"
    assert len(kim_box.sent) == 1 and choi_box.sent == []  # 김하늘 계정으로만 발송
    msg = sent_message(kim_box)
    assert msg["X-Agent-Requester"] == KIM_ID
    assert "사내 AI 에이전트를 통해 발송" in msg.get_payload(decode=True).decode("utf-8")


async def test_send_status_is_sent_for_real_client(store):
    class RealLike:  # 실제 Gmail 응답에는 simulated 표시가 없다
        async def send_message(self, raw):
            return {"id": "18c-real", "threadId": "18c-thread"}

    res = await MailService(store, lambda uid: RealLike()).send_mail(KIM_ID, [LEE], "제목", "본문")
    assert res.status == "sent"


@pytest.mark.parametrize("to,subject,body,msg", [
    ([], "제목", "본문", "최소 1명"),
    (["not-an-email"], "제목", "본문", "형식"),
    ([LEE], "", "본문", "제목"),
    ([LEE], "줄바꿈\nBcc: x@evil.com", "본문", "제목"),  # 헤더 주입 방지
    ([LEE], "제목", " ", "본문"),
])
async def test_send_mail_validation(svc, kim_box, to, subject, body, msg):
    with pytest.raises(InvalidRequest, match=msg):
        await svc.send_mail(KIM_ID, to, subject, body)
    assert kim_box.sent == []


async def test_send_mail_recipient_limits(store, clients):
    svc = MailService(store, clients, Settings(adapter="fake", max_recipients=2,
                                               allowed_recipient_domains=("acme.example",)))
    with pytest.raises(InvalidRequest, match="2명"):
        await svc.send_mail(KIM_ID, [LEE, PARK, CHOI], "제목", "본문")
    with pytest.raises(InvalidRequest, match="허용되지 않은 수신 도메인"):
        await svc.send_mail(KIM_ID, ["someone@gmail.com"], "제목", "본문")


# ── 서버 상태 페이지 ──

def test_health_and_root_page(clients, tmp_path):
    # TestClient 는 별도 이벤트 루프에서 요청을 처리해서, 루프에 묶이는 PostgreSQL 연결 풀과는 같이 쓰지 않는다 → SQLite 고정
    svc = MailService(MailStore(tmp_path / "mail.sqlite3"), clients, Settings(adapter="fake"))
    app = build_server(svc).streamable_http_app()
    with TestClient(app) as client:
        body = client.get("/health").json()
        page = client.get("/")
    assert body["adapter"] == "fake" and "가상 메일함" in body["mode"] and "connected_accounts" in body
    assert page.status_code == 200 and "정상 실행 중" in page.text
    assert all(name in page.text for name in ("search_mail", "read_mail", "send_mail"))
