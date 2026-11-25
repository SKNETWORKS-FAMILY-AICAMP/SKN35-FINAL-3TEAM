import base64

from gmail_mcp.convert import (
    MAX_MESSAGE_BODY_BYTES,
    OMITTED_NOTE,
    extract_body,
    message_from_api,
    parse_addresses,
    record_hash,
    to_record,
)
from gmail_mcp.fake import CHOI, KIM, PARK, make_message, ts


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_parse_addresses_normalizes_and_dedupes():
    assert parse_addresses("김하늘 <Sky.Kim@acme.example>, b@x.com, sky.kim@acme.example") == [KIM, "b@x.com"]
    assert parse_addresses("") == []


def test_extract_body_prefers_plain_then_html():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": b64("<p>HTML</p>")}},
        {"mimeType": "text/plain", "body": {"data": b64("PLAIN")}},
    ]}
    assert extract_body(payload) == ("PLAIN", False)
    html_only = {"mimeType": "text/html", "body": {"data": b64("<div>예산&nbsp;검토</div><script>x()</script>")}}
    assert extract_body(html_only) == ("예산\xa0검토", False)


def test_large_body_is_marked_not_silently_dropped():
    huge = "A" * (int(MAX_MESSAGE_BODY_BYTES * 4 / 3) + 8)
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("앞부분")}},
        {"mimeType": "text/plain", "body": {"data": huge}},
    ]}
    body, omitted = extract_body(payload)
    assert omitted and body.startswith("앞부분") and body.endswith(OMITTED_NOTE)


def test_message_from_api_reads_headers_and_bcc():
    m = message_from_api(make_message("x", thread_id="t", sender=f"박서연 <{PARK}>", to=[CHOI], cc=[KIM], bcc=[KIM],
                                      subject="제목", body="본문", sent_at=ts(1)))
    assert (m.sender, m.to, m.cc, m.bcc, m.thread_id, m.body) == (PARK, [CHOI], [KIM], [KIM], "t", "본문")


def test_record_hash_stable_and_changes_with_labels():
    msg = make_message("x", thread_id="t", sender=PARK, to=[CHOI], subject="s", body="b", sent_at=ts(1))
    a = to_record(message_from_api(msg), "E2002", 100)
    b = to_record(message_from_api(msg), "E2002", 100)
    assert a.content_hash == b.content_hash  # 같은 메일 → 같은 해시
    msg["labelIds"] = ["INBOX", "STARRED"]
    assert record_hash(message_from_api(msg)) != a.content_hash  # 라벨이 바뀌면 다시 저장
