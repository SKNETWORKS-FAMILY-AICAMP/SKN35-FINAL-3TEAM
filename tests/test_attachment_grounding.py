"""첨부 범위 선택·후속 질문·오답 거부. 모델과 문서는 모두 가짜 데이터로 격리한다."""

import json

import httpx
import pytest

from contracts.harness import Message
from harness.attachment_grounding import attachment_request, evidence_problem
from harness.context import fit_messages
from knowledge.ingestion.parsers import ParseResult
from tests.fakes import FakeTool, ScriptedLLM
from tests.test_uploads import HEADERS, setup  # noqa: F401
from tools.document_passages import select_passages
from tools.parse_doc import build_payload
from tools.registry import SimpleToolRegistry


def document():
    elements = []
    for heading, body in [
        ("1. 일반 사항", "사내 업무 자료를 관리한다. " * 250),
        ("2. 보존 기간", "제2조(보존 기간) 서류 보존 기간은 7년이다."),
        ("3. 예외", "제3조(예외) 별도 승인 없는 외부 반출은 금지한다."),
    ]:
        elements.extend([{"type": "section_header", "text": heading, "heading_path": [heading]},
                         {"type": "text", "text": body, "heading_path": [heading]}])
    return ParseResult("ok", file_name="규정.hwpx", elements=elements,
                       markdown="\n\n".join(e["text"] for e in elements))


class Parser:
    def parse(self, path):
        return document()


async def prepare(setup, script):  # noqa: F811
    app, svc, parser_tool = setup
    parser_tool.parser = Parser()
    searched = []
    svc.tools = SimpleToolRegistry([parser_tool, FakeTool("search_doc", log=searched)])
    svc.llm = ScriptedLLM(script)
    item = svc.uploads.create("kim", "규정.hwpx", b"test data")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return svc, item, client, searched


async def test_followup_survives_history_removal_and_does_not_search_corporate_docs(setup):  # noqa: F811
    svc, item, client, searched = await prepare(setup, [
        {"content": "보존 기간은 7년입니다. 근거: 규정.hwpx 제2조"},
        {"content": "보존 기간은 7년입니다. 근거: 규정.hwpx 제2조"},
    ])
    async with client:
        first = (await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "첨부 문서의 보존 기간은?", "file_ids": [item["file_id"]],
        })).json()
        session = svc.sessions.get_owned(first["session_id"], "kim")
        session.messages.clear()  # 컨텍스트에서 이전 첨부 메시지가 사라진 상황
        followup = (await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "보존 기간은 몇 년이야?", "session_id": first["session_id"],
        })).json()
    assert "7년" in followup["message"]
    assert svc.llm.calls[0][1] == []  # 읽은 뒤의 답변에는 도구 설명으로 컨텍스트를 낭비하지 않는다.
    assert [e["tool_name"] for e in followup["tool_events"]] == ["parse_doc"]
    assert followup["sources"][0]["partial"]
    assert followup["sources"][0]["title"] == "규정.hwpx"
    assert not searched


@pytest.mark.parametrize("bad_answer", [
    "보존 기간은 5년입니다.",
    "문서 전체 본문에 보존 기간은 명시되지 않았습니다.",
    "분석의 한계는 길이 한도 때문에 앞부분만 전달함입니다.",
    "첨부 파일의 file_id를 알려 주세요.",
    "보존 기간은 7년입니다. 근거: 규정.hwpx 제99조",
])
async def test_wrong_answers_are_repaired_or_replaced_with_real_excerpts(setup, bad_answer):  # noqa: F811
    svc, item, client, searched = await prepare(setup, [{"content": bad_answer}] * 3)
    async with client:
        response = await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "첨부 문서의 보존 기간은?", "file_ids": [item["file_id"]],
        })
    assert response.status_code == 200
    text = response.json()["message"]
    assert "7년" in text and bad_answer not in text
    assert "발췌" in text and len(svc.llm.calls) <= 3
    assert not searched


async def test_model_search_call_cannot_replace_reading_attachment(setup):  # noqa: F811
    script = [{"tool_calls": [{"id": "wrong-search", "name": "search_doc", "arguments": {"query": "보존"}}]}] * 3
    _, item, client, searched = await prepare(setup, script)
    async with client:
        result = (await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "첨부한 보존 기간 확인", "file_ids": [item["file_id"]],
        })).json()
    assert "7년" in result["message"] and not searched
    assert all(e["tool_name"] == "parse_doc" for e in result["tool_events"])


async def test_missing_topic_does_not_claim_entire_document_is_empty(setup):  # noqa: F811
    svc, item, client, searched = await prepare(setup, [])
    async with client:
        result = (await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "첨부 문서의 외계행성특별수당", "file_ids": [item["file_id"]],
        })).json()
    assert "문단을 찾지 못했습니다" in result["message"]
    assert not svc.llm.calls and not searched


async def test_attachment_read_obeys_permission_denial(setup):  # noqa: F811
    from contracts.permission import Decision
    from tests.fakes import ScriptedChecker

    svc, item, client, _ = await prepare(setup, [])
    svc.checker = ScriptedChecker({"parse_doc": Decision.DENY})
    async with client:
        result = (await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "첨부 문서의 보존 기간", "file_ids": [item["file_id"]],
        })).json()
    assert result["stop_reason"] == "denied"
    assert "7년" not in result["message"] and not svc.llm.calls


def test_query_finds_late_section_instead_of_document_prefix():
    out = build_payload("f.hwpx", document().to_dict(False), None, False, query="보존기간", max_chars=600)
    assert "7년" in out["text"] and "보존 기간" in out["text"]
    assert "일반 사항" not in out["text"]
    assert not out["read_status"]["document_complete"]


def test_section_selection_does_not_read_other_sections():
    result, status = select_passages(document().to_dict(), section="3. 예외", max_chars=600)
    assert result and all("예외" in p["location"] for p in result)
    assert "외부 반출" in result[0]["text"]


def test_json_truncation_keeps_processing_status_outside_document_text():
    payload = {"filename": "규정.hwpx", "text": "보존 기간은 7년이다. " * 1000, "truncated": False}
    messages = [Message("system", "문서만 인용"), Message("user", "보존 기간은?"),
                Message("tool", json.dumps(payload, ensure_ascii=False), tool_call_id="p")]
    fitted, report = fit_messages(messages, [], 1000, 250)
    result = json.loads(fitted[-1].content)
    assert report.truncated and report.after <= report.budget
    assert result["read_status"]["context_truncated"] and not result["read_status"]["document_complete"]
    assert "길이 한도" not in result["text"] and "처리 상태" not in result["text"]


def test_explicit_mail_action_and_corporate_search_keep_normal_harness():
    files = [{"file_id": "f.hwpx", "filename": "규정.hwpx"}]
    assert attachment_request("규정.hwpx 요약해서 메일 보내줘", files) == []
    assert attachment_request("사내 규정에서 서류 반려 검색", files) == []
    assert attachment_request("첨부 규정에서 보존 기간 확인", files) == files
    assert attachment_request("REVIEW-001 상태 조회해줘", files) == []
    assert attachment_request("오늘 날짜 알려줘", files) == []


def test_numeric_guard_allows_decimal_formatting_but_not_new_values():
    records = [{"text": "분류 정확도 93.0%, 기준은 16.5 mm 미만이다."}]
    assert evidence_problem("정확도 93%, 기준은 16.5 mm 미만입니다.", records, "정확도") is None
    assert evidence_problem("정확도 95%입니다.", records, "정확도")
    assert evidence_problem("기준은 16.5 mm 이상입니다.", records, "분류 기준")


def test_nested_rule_preserves_parent_condition_and_legitimate_absence():
    records = [{"text": "부리 깊이 < 16.5 mm이면 젠투. 그렇지 않으면 부리 길이 < 44.9 mm이면 아델리, 아니면 턱끈. 검증 세트가 없다."}]
    assert evidence_problem("깊이 16.5 mm 미만은 젠투, 길이 44.9 mm 미만은 아델리.", records, "분류 기준")
    assert evidence_problem("깊이 16.5 mm 미만은 젠투. 그 외 깊이 16.5 mm 이상에서 길이 44.9 mm 미만은 아델리.", records, "분류 기준") is None
    assert evidence_problem("검증 세트가 없습니다.", records, "한계") is None


def test_pagination_keeps_the_rest_of_a_truncated_paragraph():
    data = {"elements": [{"type": "text", "text": "가나다라마바사" * 90, "heading_path": ["긴 문단"]}]}
    first, status = select_passages(data, max_chars=256)
    assert first[0]["is_truncated"] and status["next_offset"] == 0 and status["next_char_offset"] > 0
    second, _ = select_passages(data, offset=status["next_offset"], char_offset=status["next_char_offset"], max_chars=256)
    assert second[0]["start_char"] == len(first[0]["text"])
    assert first[0]["text"] + second[0]["text"] == data["elements"][0]["text"][:len(first[0]["text"]) + len(second[0]["text"])]


def test_excerpt_boundary_does_not_create_a_partial_numeric_fact():
    data = {"elements": [{"type": "text", "text": "가" * 235 + "\n정확도 93.0%이다.", "heading_path": ["통계"]}]}
    first, status = select_passages(data, max_chars=256)
    assert "93" not in first[0]["text"] and not first[0]["text"].endswith("9")
    second, _ = select_passages(data, offset=status["next_offset"], char_offset=status["next_char_offset"], max_chars=256)
    assert "93.0%" in second[0]["text"]


async def test_deleted_attachment_is_not_replaced_with_corporate_search(setup):  # noqa: F811
    svc, item, client, searched = await prepare(setup, [{"content": "보존 기간은 7년입니다."}])
    async with client:
        first = (await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "첨부 문서의 보존 기간", "file_ids": [item["file_id"]],
        })).json()
        (svc.uploads.directory / item["file_id"]).unlink()
        svc.llm = ScriptedLLM([])
        second = (await client.post("/api/chat/messages", headers=HEADERS, json={
            "content": "이 문서 보존 기간 다시 알려줘", "session_id": first["session_id"],
        })).json()
    assert "더 이상 열 수 없습니다" in second["message"] and not searched and not svc.llm.calls


def test_preview_source_is_distinct_from_unread_search_result():
    from services.chat import sources

    payload = {"matches": [{"title": "규정", "location": "제2조", "preview": "보존 기간은 7년"}]}
    source = sources([Message("tool", json.dumps(payload, ensure_ascii=False))])[0]
    assert source.previewed and not source.opened
