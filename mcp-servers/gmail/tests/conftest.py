import pytest

from gmail_mcp.fake import CHOI, KIM, LEE, PARK, FakeGmailClient, make_message, ts


@pytest.fixture
def mailbox() -> FakeGmailClient:
    c = FakeGmailClient()
    c.add_thread("t1", make_message("m1", sender=f"김하늘 <{KIM}>", to=[LEE], subject="인사평가 일정",
                                    body="12월 8일 시작", sent_at=ts(10)))
    c.add_thread("t2", make_message("m2", sender=PARK, to=[CHOI], cc=[KIM], subject="경비 정산 마감",
                                    body="11월 28일까지", sent_at=ts(12)))
    c.add_thread("t3", make_message("m3", sender=PARK, to=[CHOI], subject="재무팀 내부 회의",
                                    body="<p>예산 <b>검토</b></p>", sent_at=ts(15), html=True))
    return c
