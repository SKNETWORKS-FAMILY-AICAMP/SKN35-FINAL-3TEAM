"""Gmail API 원본(dict) → MailMessage / MailRecord.

설계 참고: Onyx Gmail 커넥터(MIT, backend/onyx/connectors/gmail/connector.py)의
헤더 처리 · 본문 추출 구조. 코드는 이 프로젝트에 맞게 새로 작성했다.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from .models import MailMessage, MailRecord

MAX_MESSAGE_BODY_BYTES = 10 * 1024 * 1024  # 이보다 큰 본문 파트는 생략하고 표시한다
OMITTED_NOTE = "[본문 일부가 너무 커서 생략됨]"
NO_SUBJECT = "(제목 없음)"


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


def b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", "", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def extract_body(payload: dict[str, Any]) -> tuple[str, bool]:
    """(본문, 생략 여부). text/plain 우선, 없으면 text/html 을 텍스트로.

    큰 파트는 조용히 건너뛰지 않고 생략 표시를 남긴다.
    """
    plain: list[str] = []
    rich: list[str] = []
    omitted = False

    def walk(part: dict[str, Any]) -> None:
        nonlocal omitted
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            if len(data) * 3 / 4 > MAX_MESSAGE_BODY_BYTES:  # base64 길이로 크기 추정
                omitted = True
            else:
                try:
                    text = b64decode(data).decode("utf-8", errors="replace")
                except (ValueError, TypeError):
                    omitted = True
                else:
                    (plain if mime == "text/plain" else rich).append(text)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    body = "\n".join(plain).strip() if plain else "\n".join(_html_to_text(t) for t in rich).strip()
    if omitted:
        body = f"{body}\n{OMITTED_NOTE}".strip()
    return body, omitted


def message_from_api(message: dict[str, Any], *, with_body: bool = True) -> MailMessage:
    headers = message.get("payload", {}).get("headers", [])
    senders = parse_addresses(header(headers, "From"))
    body, omitted = extract_body(message.get("payload", {})) if with_body else ("", False)
    return MailMessage(
        message_id=message["id"],
        thread_id=message.get("threadId", ""),
        sender=senders[0] if senders else "",
        to=parse_addresses(header(headers, "To")),
        cc=parse_addresses(header(headers, "Cc")),
        bcc=parse_addresses(header(headers, "Bcc")),
        subject=header(headers, "Subject"),
        sent_at=parse_date(header(headers, "Date")),
        labels=sorted(message.get("labelIds", [])),
        body=body,
        body_omitted=omitted,
    )


def record_hash(message: MailMessage) -> str:
    """저장할 내용의 해시. 같은 메일을 다시 가져와도 내용이 같으면 DB 를 다시 쓰지 않는다."""
    payload = json.dumps(message.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_record(message: MailMessage, owner_user_id: str, size_bytes: int) -> MailRecord:
    return MailRecord(**message.model_dump(), owner_user_id=owner_user_id, size_bytes=size_bytes,
                      content_hash=record_hash(message))
