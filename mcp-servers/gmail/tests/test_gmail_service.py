import base64
import email

import pytest
from gmail_mcp.fake import CHOI, KIM, LEE, PARK

from gmail_mcp.config import Settings
from gmail_mcp.service import NOT_FOUND, InvalidRequest, MailService


@pytest.fixture
def svc(mailbox):
    return MailService(mailbox)


async def test_search_only_returns_my_threads(svc):
    res = await svc.search_mail(KIM)
    assert {t.thread_id for t in res.threads} == {"t1", "t2"}  # t3(재무팀 내부)는 참여자가 아님
    assert {t.thread_id for t in (await svc.search_mail(CHOI)).threads} == {"t2", "t3"}


async def test_search_with_words(svc):
    assert [t.thread_id for t in (await svc.search_mail(KIM, "경비")).threads] == ["t2"]


async def test_query_cannot_break_participant_filter(svc, mailbox):
    # 중괄호로 참여자 조건을 깨려는 시도 → 중괄호 제거
    await svc.search_mail(LEE, "} OR {in:anywhere")
    assert "{" not in mailbox.list_calls[-1][0].split("}", 1)[1]


async def test_search_double_checks_participants(svc, mailbox):
    async def leaky_list(query, token, max_results=100):  # Gmail 이 엉뚱한 결과를 줘도
        return ["t1", "t2", "t3"], None
    mailbox.list_threads = leaky_list
    assert {t.thread_id for t in (await svc.search_mail(LEE)).threads} == {"t1"}


async def test_read_mail_permission(svc):
    thread = await svc.read_mail(CHOI, "t3")
    assert "예산" in thread.messages[0].body
    with pytest.raises(InvalidRequest, match=NOT_FOUND):
        await svc.read_mail(KIM, "t3")  # 참여자가 아니면 존재 여부도 드러내지 않음
    with pytest.raises(InvalidRequest, match=NOT_FOUND):
        await svc.read_mail(KIM, "no-such-thread")


async def test_read_mail_truncates_long_body(mailbox):
    svc = MailService(mailbox, Settings(adapter="fake", max_read_body_chars=3))
    thread = await svc.read_mail(KIM, "t1")
    assert thread.truncated and thread.messages[0].body.startswith("12월")


async def test_requester_email_required(svc):
    with pytest.raises(InvalidRequest, match="요청자"):
        await svc.search_mail("")


def sent_message(mailbox):
    return email.message_from_bytes(base64.urlsafe_b64decode(mailbox.sent[-1]))


async def test_send_mail(svc, mailbox):
    res = await svc.send_mail("E1001", KIM, [" Doyun.Lee@acme.example "], "회의 일정", "내일 10시", cc=[PARK])
    assert res.to == [LEE] and res.cc == [PARK]
    msg = sent_message(mailbox)
    assert msg["Reply-To"] == KIM and msg["X-Agent-Requester"] == "E1001"
    assert "사내 AI 에이전트가 발송" in msg.get_payload(decode=True).decode("utf-8")
    assert res.status == "simulated"  # 가상 메일함은 실제로 발송하지 않았음을 결과에 표시


async def test_send_status_is_sent_for_real_client():
    class RealLike:  # 실제 Gmail 응답에는 simulated 표시가 없다
        async def get_profile_email(self):
            return "team@acme.example"

        async def send_message(self, raw):
            return {"id": "18c-real", "threadId": "18c-thread"}

    res = await MailService(RealLike()).send_mail("E1001", KIM, [LEE], "제목", "본문")
    assert res.status == "sent"


async def test_health_reports_mode(mailbox):
    from starlette.testclient import TestClient

    from gmail_mcp.server import build_server

    app = build_server(MailService(mailbox)).streamable_http_app()
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["adapter"] == "fake" and "가상 메일함" in body["mode"]


async def test_root_shows_status_page_instead_of_404(mailbox):
    from starlette.testclient import TestClient

    from gmail_mcp.server import build_server

    app = build_server(MailService(mailbox)).streamable_http_app()
    with TestClient(app) as client:
        res = client.get("/")
    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
    page = res.text
    assert "정상 실행 중" in page and "가상 메일함" in page
    assert all(name in page for name in ("search_mail", "read_mail", "send_mail"))
    assert "http://testserver/mcp" in page


@pytest.mark.parametrize("to,subject,body,msg", [
    ([], "제목", "본문", "최소 1명"),
    (["not-an-email"], "제목", "본문", "형식"),
    ([LEE], "", "본문", "제목"),
    ([LEE], "줄바꿈\nBcc: x@evil.com", "본문", "제목"),  # 헤더 주입 방지
    ([LEE], "제목", " ", "본문"),
])
async def test_send_mail_validation(svc, mailbox, to, subject, body, msg):
    with pytest.raises(InvalidRequest, match=msg):
        await svc.send_mail("E1001", KIM, to, subject, body)
    assert mailbox.sent == []


async def test_send_mail_recipient_limits(mailbox):
    svc = MailService(mailbox, Settings(adapter="fake", max_recipients=2, allowed_recipient_domains=("acme.example",)))
    with pytest.raises(InvalidRequest, match="2명"):
        await svc.send_mail("E1001", KIM, [LEE, PARK, CHOI], "제목", "본문")
    with pytest.raises(InvalidRequest, match="허용되지 않은 수신 도메인"):
        await svc.send_mail("E1001", KIM, ["someone@gmail.com"], "제목", "본문")
