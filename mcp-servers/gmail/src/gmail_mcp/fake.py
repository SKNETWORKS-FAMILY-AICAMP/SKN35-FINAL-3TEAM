"""가상 메일함 — Gmail API 응답 형태를 흉내낸다. 테스트와 팀 계정 없이 하는 로컬 시연에 사용한다.

검색어는 이 프로젝트가 실제로 만드는 형태만 지원한다:
  after:<epoch>  before:<epoch>  {from:a to:a cc:a bcc:a}  일반 단어(제목·본문 포함 여부)
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any

TEAM_ACCOUNT = "team@acme.example"

# 가상 회사 직원 메일 주소 (hr 서버의 가상 데이터와 같은 사람들)
KIM = "sky.kim@acme.example"
LEE = "doyun.lee@acme.example"
PARK = "seoyeon.park@acme.example"
CHOI = "minjun.choi@acme.example"


def ts(day: int, hour: int = 0) -> datetime:
    """2026년 11월 day일 hour시 (UTC). 테스트·시연 데이터용."""
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def make_message(
    message_id: str,
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    sent_at: datetime,
    cc: list[str] | None = None,
    html: bool = False,
) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": ", ".join(to)},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": format_datetime(sent_at)},
    ]
    if cc:
        headers.append({"name": "Cc", "value": ", ".join(cc)})
    mime = "text/html" if html else "text/plain"
    return {
        "id": message_id,
        "internalDate": str(int(sent_at.timestamp() * 1000)),
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [{"mimeType": mime, "body": {"data": _b64(body)}}],
        },
    }


class FakeGmailClient:
    simulated = True  # 서버·결과에 "가상 메일함" 임을 표시하는 데 쓴다

    def __init__(self, owner: str = TEAM_ACCOUNT) -> None:
        self.owner = owner
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.sent: list[str] = []
        self.fail_threads: set[str] = set()
        self.list_calls: list[tuple[str | None, str | None]] = []

    def add_thread(self, thread_id: str, *messages: dict[str, Any]) -> None:
        self.threads.setdefault(thread_id, []).extend(messages)

    async def get_profile_email(self) -> str:
        return self.owner

    def _matches(self, messages: list[dict[str, Any]], query: str | None) -> bool:
        if not query:
            return True
        latest = max(int(m["internalDate"]) for m in messages) / 1000
        q = query
        if m := re.search(r"after:(\d+)", q):
            if latest <= int(m.group(1)):
                return False
        if m := re.search(r"before:(\d+)", q):
            if latest >= int(m.group(1)):
                return False
        if m := re.search(r"\{([^}]*)\}", q):
            emails = set(re.findall(r"(?:from|to|cc|bcc):(\S+)", m.group(1)))
            headers = " ".join(h["value"].lower() for msg in messages for h in msg["payload"]["headers"]
                               if h["name"] in ("From", "To", "Cc", "Bcc"))
            if not any(e.lower() in headers for e in emails):
                return False
        words = re.sub(r"after:\d+|before:\d+|\{[^}]*\}|[()]", " ", q).split()
        text = " ".join(
            h["value"] for msg in messages for h in msg["payload"]["headers"] if h["name"] == "Subject"
        ) + " " + " ".join(base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", "replace")
                           for msg in messages for p in msg["payload"]["parts"])
        return all(w.lower() in text.lower() for w in words)

    async def list_threads(self, query, page_token, max_results=100):
        self.list_calls.append((query, page_token))
        ids = sorted(
            (tid for tid, msgs in self.threads.items() if self._matches(msgs, query)),
            key=lambda t: -max(int(m["internalDate"]) for m in self.threads[t]),  # 최신순 (Gmail 과 같게)
        )
        offset = int(page_token or 0)
        page = ids[offset : offset + max_results]
        nxt = offset + max_results
        return page, (str(nxt) if nxt < len(ids) else None)

    async def get_thread(self, thread_id, *, metadata_only=False):
        if thread_id in self.fail_threads:
            raise ConnectionError("가상 오류")
        messages = self.threads.get(thread_id)
        if messages is None:
            return None
        if metadata_only:
            messages = [{**m, "payload": {"headers": m["payload"]["headers"]}} for m in messages]
        return {"id": thread_id, "messages": messages}

    async def send_message(self, raw):
        self.sent.append(raw)
        n = len(self.sent)
        return {"id": f"sent-{n}", "threadId": f"sent-thread-{n}", "simulated": True}


def sample_mailbox(client: FakeGmailClient) -> FakeGmailClient:
    """가상 회사 메일 몇 통 (로컬 시연용). 실제 인물·사내 정보가 아니다."""
    t = lambda d, h: datetime(2026, 11, d, h, 0, tzinfo=timezone.utc)  # noqa: E731
    client.add_thread("18c1a0000000a001", make_message(
        "m1", sender="김하늘 <sky.kim@acme.example>", to=["doyun.lee@acme.example"],
        subject="12월 인사평가 일정 안내", body="12월 8일부터 인사평가가 시작됩니다.", sent_at=t(17, 1)))
    client.add_thread("18c1a0000000a002", make_message(
        "m2", sender="박서연 <seoyeon.park@acme.example>", to=["minjun.choi@acme.example"],
        cc=["sky.kim@acme.example"], subject="11월 경비 정산 마감", body="경비 정산은 11월 28일까지입니다.",
        sent_at=t(18, 2)))
    return client
