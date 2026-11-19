import base64

from gmail_mcp.fake import CHOI, KIM, LEE, PARK, ts

from gmail_mcp.convert import extract_body, parse_addresses, thread_to_document, thread_to_slim
from gmail_mcp.fake import TEAM_ACCOUNT, make_message


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_parse_addresses_normalizes_and_dedupes():
    assert parse_addresses('김하늘 <Sky.Kim@acme.example>, b@x.com, sky.kim@acme.example') == [KIM, "b@x.com"]
    assert parse_addresses("") == []


def test_extract_body_prefers_plain_then_html():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": b64("<p>HTML</p>")}},
        {"mimeType": "text/plain", "body": {"data": b64("PLAIN")}},
    ]}
    assert extract_body(payload) == "PLAIN"
    html_only = {"mimeType": "text/html", "body": {"data": b64("<div>예산&nbsp;검토</div><script>x()</script>")}}
    assert extract_body(html_only) == "예산\xa0검토"


def test_thread_to_document():
    thread = {"id": "t9", "messages": [
        make_message("a", sender=PARK, to=[CHOI], subject="첫 메일", body="본문1", sent_at=ts(1)),
        make_message("b", sender=CHOI, to=[PARK], cc=[KIM], subject="Re: 첫 메일", body="본문2", sent_at=ts(2)),
    ]}
    doc = thread_to_document(thread, TEAM_ACCOUNT)
    assert doc.doc_id == "gmail:t9" and doc.title == "첫 메일"
    assert (doc.created_at, doc.updated_at) == (ts(1), ts(2))
    assert doc.primary_owners == [CHOI, PARK]
    assert doc.access.user_emails == tuple(sorted({PARK, CHOI, KIM, TEAM_ACCOUNT}))
    assert not doc.access.allows(LEE) and doc.access.allows(KIM.upper())
    assert "본문1" in doc.to_text() and "본문2" in doc.to_text()


def test_empty_thread_returns_none():
    assert thread_to_document({"id": "t0", "messages": []}, TEAM_ACCOUNT) is None


def test_slim_has_access_without_body():
    thread = {"id": "t9", "messages": [make_message("a", sender=PARK, to=[CHOI], subject="s", body="b", sent_at=ts(1))]}
    slim = thread_to_slim(thread, TEAM_ACCOUNT)
    assert slim.doc_id == "gmail:t9" and slim.access.allows(CHOI)
