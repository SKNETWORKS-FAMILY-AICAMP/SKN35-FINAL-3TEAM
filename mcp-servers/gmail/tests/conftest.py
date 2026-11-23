import pytest

from gmail_mcp.config import Settings
from gmail_mcp.fake import CHOI, CHOI_ID, KIM, KIM_ID, LEE, PARK, FakeGmailClient, make_message, ts
from gmail_mcp.google_auth import AccountNotConnected
from gmail_mcp.service import MailService
from gmail_mcp.store import MailStore


@pytest.fixture
def store(tmp_path) -> MailStore:
    return MailStore(tmp_path / "mail.sqlite3")


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
