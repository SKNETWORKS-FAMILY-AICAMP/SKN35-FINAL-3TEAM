"""Gmail API 원본(dict) → MailMessage / MailDocument 변환.

설계 참고: Onyx Gmail 커넥터(MIT, backend/onyx/connectors/gmail/connector.py)의
thread_to_document · message_to_section · _get_message_body 구조. 코드는 이 프로젝트에 맞게 새로 작성했다.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from .models import MailAccess, MailDocument, MailMessage, SlimMailDocument

MAX_MESSAGE_BODY_BYTES = 10 * 1024 * 1024  # 이보다 큰 본문 파트는 건너뜀 (Onyx 와 같은 기준)
NO_SUBJECT = "(제목 없음)"


def doc_id_for(thread_id: str) -> str:
    return f"gmail:{thread_id}"


def thread_link(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"


def header(headers: list[dict[str, str]], name: str) -> str:
    return next((h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()), "")


def parse_addresses(value: str) -> list[str]:
    """'홍길동 <a@b.com>, c@d.com' → ['a@b.com', 'c@d.com'] (소문자, 중복 제거, 순서 유지)."""
    seen: dict[str, None] = {}
    for _, addr in getaddresses([value]) if value else []:
        addr = addr.strip().lower()
        if "@" in addr:
            seen.setdefault(addr, None)
    return list(seen)


def parse_date(value: str) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def _decode(data: str) -> str | None:
    if len(data) * 3 / 4 > MAX_MESSAGE_BODY_BYTES:  # base64 길이로 크기 추정
        return None
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return None


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", "", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def extract_body(payload: dict[str, Any]) -> str:
    """MIME 트리에서 text/plain 을 우선 찾고, 없으면 text/html 을 텍스트로 바꿔 쓴다."""
    plain, rich = [], []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            text = _decode(data)
            if text:
                (plain if mime == "text/plain" else rich).append(text)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    return "\n".join(_html_to_text(t) for t in rich).strip()


def message_from_api(message: dict[str, Any], *, with_body: bool = True) -> MailMessage:
    headers = message.get("payload", {}).get("headers", [])
    senders = parse_addresses(header(headers, "From"))
    return MailMessage(
        message_id=message["id"],
        sender=senders[0] if senders else "",
        to=parse_addresses(header(headers, "To")),
        cc=parse_addresses(header(headers, "Cc")),
        bcc=parse_addresses(header(headers, "Bcc")),
        subject=header(headers, "Subject"),
        sent_at=parse_date(header(headers, "Date")),
        labels=list(message.get("labelIds", [])),
        body=extract_body(message.get("payload", {})) if with_body else "",
    )


def build_access(messages: list[MailMessage], mailbox_owner: str) -> MailAccess:
    """스레드 참여자 전원 + 메일함 소유자(팀 계정)."""
    emails = {mailbox_owner}
    for m in messages:
        emails |= m.participants
    return MailAccess(user_emails=tuple(emails))


def thread_to_document(thread: dict[str, Any], mailbox_owner: str) -> MailDocument | None:
    thread_id = thread.get("id")
    if not thread_id:
        raise ValueError("스레드 ID가 없습니다")
    messages = [message_from_api(m) for m in thread.get("messages", [])]
    if not messages:
        return None
    dates = [m.sent_at for m in messages if m.sent_at]
    return MailDocument(
        doc_id=doc_id_for(thread_id),
        thread_id=thread_id,
        title=next((m.subject for m in messages if m.subject), NO_SUBJECT),
        link=thread_link(thread_id),
        created_at=min(dates) if dates else None,
        updated_at=max(dates) if dates else None,
        primary_owners=sorted({m.sender for m in messages if m.sender}),
        secondary_owners=sorted({a for m in messages for a in [*m.to, *m.cc, *m.bcc]}),
        messages=messages,
        access=build_access(messages, mailbox_owner),
    )


def thread_to_slim(thread: dict[str, Any], mailbox_owner: str) -> SlimMailDocument | None:
    messages = [message_from_api(m, with_body=False) for m in thread.get("messages", [])]
    if not thread.get("id") or not messages:
        return None
    return SlimMailDocument(doc_id=doc_id_for(thread["id"]), access=build_access(messages, mailbox_owner))
