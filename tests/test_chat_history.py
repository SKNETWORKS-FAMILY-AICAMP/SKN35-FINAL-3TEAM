"""최근 대화 목록과 지난 대화 다시 열기 (COM-06). 메모리 저장소로 확인한다. DB 저장소는 tests/db/test_db_stores.py."""

from __future__ import annotations

import json

from contracts.harness import LLMToolCall, Message
from services.chat import past_turns
from services.uploads import attachment_context, split_attachment_context
from tests.test_api import KIM, make, tool_call


def test_attachment_context_round_trip():
    files = [{"file_id": "a1.pdf", "filename": "보고서.pdf"}]
    assert split_attachment_context(attachment_context("요약해 줘", files)) == ("요약해 줘", files)
    assert split_attachment_context("그냥 질문") == ("그냥 질문", [])
    assert split_attachment_context("질문\n\n[사용자 첨부 파일 목록]\n깨진 값")[1] == []


def test_past_turns_groups_by_user_message():
    search = json.dumps({"matches": [{"title": "여신규정", "location": "제7조"}]}, ensure_ascii=False)
    messages = [
        Message("user", "시설자금 몇 %?"),
        Message("assistant", "", tool_calls=(LLMToolCall("c1", "search_doc", {"query": "시설자금"}),)),
        Message("tool", search, tool_call_id="c1"),
        Message("assistant", "80%입니다"),
        Message("user", "고마워"),
        Message("assistant", "천만에요"),
    ]
    first, second = past_turns(messages, {"search_doc": "사내 문서 검색"})
    assert (first.question, first.message) == ("시설자금 몇 %?", "80%입니다")
    assert [(e.display_name, e.ok) for e in first.tool_events] == [("사내 문서 검색", True)]
    assert [(s.title, s.location, s.opened) for s in first.sources] == [("여신규정", "제7조", False)]
    assert (second.question, second.message, second.tool_events) == ("고마워", "천만에요", [])


async def test_list_and_reopen_through_api(tmp_path):
    client, _ = make([tool_call("c1", "add", a=3, b=4), {"content": "7입니다"}, {"content": "다른 답"}], tmp_path)
    assert (await client.get("/api/chat/sessions", headers=KIM)).json() == []
    first = (await client.post("/api/chat/messages", json={"content": "3+4\n둘째 줄"}, headers=KIM)).json()
    second = (await client.post("/api/chat/messages", json={"content": "다른 질문"}, headers=KIM)).json()

    listed = (await client.get("/api/chat/sessions", headers=KIM)).json()
    assert [(x["session_id"], x["title"], x["agent_id"]) for x in listed] == [
        (second["session_id"], "다른 질문", None), (first["session_id"], "3+4", None)]   # 최근 것부터

    turns = (await client.get(f"/api/chat/sessions/{first['session_id']}/turns", headers=KIM)).json()
    assert len(turns) == 1 and turns[0]["question"] == "3+4\n둘째 줄" and turns[0]["message"] == "7입니다"
    assert turns[0]["tool_events"] == first["tool_events"]

    park = {"X-User-Id": "park"}
    assert (await client.get("/api/chat/sessions", headers=park)).json() == []
    assert (await client.get(f"/api/chat/sessions/{first['session_id']}/turns", headers=park)).status_code == 404
    assert turns[0]["denial"] is None


async def test_reopened_turn_keeps_denial(tmp_path):
    """권한 거부로 멈춘 질문은 다시 열어도 같은 거부 안내가 나온다 (몇 번째 질문인지도 맞게)."""
    client, _ = make([{"content": "안녕하세요"}, tool_call("c1", "multiply", a=2, b=3)], tmp_path)
    first = (await client.post("/api/chat/messages", json={"content": "안녕"}, headers=KIM)).json()
    denied = (await client.post("/api/chat/messages", json={"content": "2*3", "session_id": first["session_id"]},
                                headers=KIM)).json()
    assert denied["stop_reason"] == "denied"
    turns = (await client.get(f"/api/chat/sessions/{first['session_id']}/turns", headers=KIM)).json()
    assert [t["denial"] for t in turns] == [None, denied["denial"]]
    assert turns[1]["message"] == denied["message"]
