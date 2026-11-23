"""가상 메일함 — 직원 한 명의 Gmail 을 흉내낸다. 테스트와 실제 계정 없이 하는 로컬 시연에 사용한다.

검색어는 동기화가 실제로 만드는 형태만 지원한다:  after:<epoch>  before:<epoch>
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any

# 가상 회사 직원 (사번 ↔ 메일 주소). 실제 인물이 아니다.
KIM, KIM_ID = "sky.kim@acme.example", "E1001"
LEE, LEE_ID = "doyun.lee@acme.example", "E1002"
PARK, PARK_ID = "seoyeon.park@acme.example", "E2001"
CHOI, CHOI_ID = "minjun.choi@acme.example", "E2002"


def ts(day: int, hour: int = 0) -> datetime:
    """2026년 11월 day일 hour시 (UTC). 테스트·시연 데이터용."""
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def make_message(
    message_id: str,
    *,
    thread_id: str,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    sent_at: datetime,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    labels: list[str] | None = None,
    size: int | None = None,
) -> dict[str, Any]:
    """Gmail messages.get(format=full) 와 같은 모양."""
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": ", ".join(to)},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": format_datetime(sent_at)},
    ]
    if cc:
        headers.append({"name": "Cc", "value": ", ".join(cc)})
    if bcc:  # 내가 보낸 메일에만 남는 헤더
        headers.append({"name": "Bcc", "value": ", ".join(bcc)})
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": str(int(sent_at.timestamp() * 1000)),
        "labelIds": labels or ["INBOX"],
        "sizeEstimate": size if size is not None else 500 + len(body.encode("utf-8")),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [{"mimeType": "text/html" if html else "text/plain", "body": {"data": _b64(body)}}],
        },
    }


class FakeGmailClient:
    simulated = True  # 서버·결과에 "가상 메일함" 임을 표시하는 데 쓴다

    def __init__(self, owner_email: str) -> None:
        self.owner_email = owner_email
        self.messages: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []
        self.fail_ids: set[str] = set()  # 이 메일을 가져오면 오류
        self.vanish_ids: set[str] = set()  # 목록에는 나오지만 가져오면 사라진 메일
        self.list_calls: list[tuple[str | None, str | None]] = []

    def add(self, *messages: dict[str, Any]) -> FakeGmailClient:
        for m in messages:
            self.messages[m["id"]] = m
        return self

    def remove(self, message_id: str) -> None:
        self.messages.pop(message_id, None)

    async def get_profile_email(self) -> str:
        return self.owner_email

    async def list_messages(self, query, page_token, max_results=100):
        self.list_calls.append((query, page_token))
        after = int(m.group(1)) if query and (m := re.search(r"after:(\d+)", query)) else None
        before = int(m.group(1)) if query and (m := re.search(r"before:(\d+)", query)) else None
        ids = [mid for mid, msg in sorted(self.messages.items(), key=lambda kv: -int(kv[1]["internalDate"]))
               if (after is None or int(msg["internalDate"]) / 1000 > after)
               and (before is None or int(msg["internalDate"]) / 1000 < before)]
        offset = int(page_token or 0)
        nxt = offset + max_results
        return ids[offset:nxt], (str(nxt) if nxt < len(ids) else None)

    async def get_message(self, message_id, *, format="full"):
        if message_id in self.fail_ids:
            raise ConnectionError("가상 오류")
        if message_id in self.vanish_ids or message_id not in self.messages:
            return None
        msg = self.messages[message_id]
        if format == "metadata":
            return {**msg, "payload": {"headers": msg["payload"]["headers"]}}
        return msg

    async def send_message(self, raw):
        self.sent.append(raw)
        n = len(self.sent)
        return {"id": f"sent-{n}", "threadId": f"sent-thread-{n}", "simulated": True}


def sample_mailboxes() -> dict[str, FakeGmailClient]:
    """직원별 가상 메일함 (로컬 시연용)."""
    kim = FakeGmailClient(KIM).add(
        make_message("k1", thread_id="kt1", sender=f"김하늘 <{KIM}>", to=[LEE], subject="12월 인사평가 일정 안내",
                     body="12월 8일부터 인사평가가 시작됩니다.", sent_at=ts(17, 1), labels=["SENT"]),
        make_message("k2", thread_id="kt2", sender=PARK, to=[CHOI], cc=[KIM], subject="11월 경비 정산 마감",
                     body="경비 정산은 11월 28일까지입니다.", sent_at=ts(18, 2)),
    )
    choi = FakeGmailClient(CHOI).add(
        make_message("c1", thread_id="ct1", sender=PARK, to=[CHOI], cc=[KIM], subject="11월 경비 정산 마감",
                     body="경비 정산은 11월 28일까지입니다.", sent_at=ts(18, 2)),
        make_message("c2", thread_id="ct2", sender=PARK, to=[CHOI], subject="연봉 상한 확정",
                     body="올해 연봉 상한은 5% 입니다.", sent_at=ts(19, 3)),
    )
    return {KIM_ID: kim, CHOI_ID: choi, LEE_ID: FakeGmailClient(LEE), PARK_ID: FakeGmailClient(PARK)}
