"""업로드 문서 읽기 툴(parse_doc). 모델 없이 도는 HWPX·거절 경로만 쓴다."""

from __future__ import annotations

import json

import pytest

from contracts.permission import Principal, ToolCall
from tests.knowledge.ingestion.parsers.test_pipeline import _hwpx  # 제목 1 + 본문 1 + 1x2 표인 가장 작은 HWPX
from tools import parse_doc as pd
from tools.parse_doc import ParseDocTool

KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})


def call(args: dict) -> ToolCall:
    return ToolCall("c1", "parse_doc", args, KIM, "s1")


@pytest.fixture
def uploads(tmp_path):
    (tmp_path / "규정.hwpx").write_bytes(_hwpx())
    (tmp_path / "이상한.bin").write_bytes(b"\x00\x01\x02 not a doc")
    return tmp_path


async def test_hwpx_parsed_then_read_from_cache(uploads):
    tool = ParseDocTool(uploads)
    first = await tool.run(call({"file_id": "규정.hwpx"}))
    assert first.ok
    body = json.loads(first.content)
    assert "제1장 총칙" in body["text"] and "| 구분" in body["text"]
    assert body["tables"] == 1 and body["truncated"] is False and body["cached"] is False
    assert first.data["elements"]  # 화면용 원본 요소 목록
    assert (uploads / "parsed" / "규정.hwpx.json").is_file()  # 파서 입출력 계약대로 원본 옆에 저장

    second = await tool.run(call({"file_id": "규정.hwpx"}))
    assert second.ok and json.loads(second.content)["cached"] is True


@pytest.mark.parametrize("file_id", ["없는파일.pdf", "../규정.hwpx", "sub/규정.hwpx", ".env", "", 3])
async def test_only_files_directly_in_upload_dir(uploads, file_id):
    r = await ParseDocTool(uploads).run(call({"file_id": file_id}))
    assert not r.ok and "찾을 수 없습니다" in r.content  # 폴더 밖 경로는 있는지도 알려주지 않는다


async def test_rejected_doc_reports_reason(uploads):
    r = await ParseDocTool(uploads).run(call({"file_id": "이상한.bin"}))
    assert not r.ok and r.content.startswith("문서를 읽지 못했습니다") and r.data["status"] == "rejected"
    assert not (uploads / "parsed" / "이상한.bin.json").exists()  # 실패 결과는 캐시하지 않는다


async def test_long_text_is_truncated(uploads, monkeypatch):
    monkeypatch.setattr(pd, "MAX_CHARS", 10)
    body = json.loads((await ParseDocTool(uploads).run(call({"file_id": "규정.hwpx"}))).content)
    assert body["truncated"] is True and len(body["text"]) == 10


@pytest.mark.parametrize("pages", [[0, 1], [3, 2], [1], "1-2"])
async def test_bad_pages_argument_is_error(uploads, pages):
    r = await ParseDocTool(uploads).run(call({"file_id": "규정.hwpx", "pages": pages}))
    assert not r.ok and "pages" in r.content


def test_page_range_keeps_only_overlapping_elements():
    # 쪽 범위: 그 쪽에 걸친 요소만, 여러 쪽 표는 한 쪽이라도 겹치면 넣는다
    d = {"pages": 3, "markdown": "전체", "elements": [
        {"type": "section_header", "text": "1쪽 제목", "page": 1, "pages": [1]},
        {"type": "text", "text": "2쪽 본문", "page": 2, "pages": [2]},
        {"type": "table", "text": "", "page": 2, "pages": [2, 3], "table": {"markdown": "| 이어진 표 |"}},
    ]}
    out = pd.build_payload("a.pdf", d, (3, 3), cached=True)
    assert out["text"] == "| 이어진 표 |" and out["tables"] == 1
    out = pd.build_payload("a.pdf", d, (1, 2), cached=True)
    assert out["text"].startswith("## 1쪽 제목") and "2쪽 본문" in out["text"]


@pytest.mark.parametrize("content", ["[]", "null", '{"status":"ok","parser_version":"wrong"}'])
async def test_malformed_cache_is_reparsed(uploads, content):
    parsed = uploads / "parsed"
    parsed.mkdir()
    (parsed / "규정.hwpx.json").write_text(content, encoding="utf-8")
    result = await ParseDocTool(uploads).run(call({"file_id": "규정.hwpx"}))
    assert result.ok and not result.data["cached"]


async def test_parser_stays_serialized_after_waiter_is_cancelled(uploads):
    import asyncio
    import threading
    from knowledge.ingestion.parsers import ParseResult

    started = threading.Event()
    release = threading.Event()

    class BlockingParser:
        active = 0
        peak = 0

        def parse(self, path):
            self.active += 1
            self.peak = max(self.peak, self.active)
            started.set()
            try:
                assert release.wait(5)
                return ParseResult("ok", file_name=path.name, markdown="본문")
            finally:
                self.active -= 1

    parser = BlockingParser()
    tool = ParseDocTool(uploads, parser=parser)
    first = asyncio.create_task(tool.run(call({"file_id": "규정.hwpx"})))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(tool.run(call({"file_id": "규정.hwpx"})))
        await asyncio.sleep(.05)
    finally:
        release.set()
    assert (await second).ok
    assert parser.peak == 1


async def test_parser_via_agent_chat_api(uploads, tmp_path):
    import httpx
    from main import create_app
    from permission import create_checker
    from permission.approval import FutureApprovalHandler
    from permission.audit import JsonlAuditLogger
    from permission.policy import load_policy
    from services.container import AppServices
    from services.users import UserDirectory
    from tests.fakes import ScriptedLLM
    from tools.registry import SimpleToolRegistry

    rules, default = load_policy("config/permissions.yaml")
    svc = AppServices(
        llm=ScriptedLLM([
            {"tool_calls": [{"id": "parse-1", "name": "parse_doc", "arguments": {"file_id": "규정.hwpx"}}]},
            {"content": "표와 본문을 읽었습니다."},
        ]),
        tools=SimpleToolRegistry([ParseDocTool(uploads)]),
        checker=create_checker(rules=rules, default=default),
        approvals=FutureApprovalHandler(1), audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory({"kim": KIM}),
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": "kim"}
        agent = await client.post("/api/agents", headers=headers, json={
            "name": "문서 읽기", "system_prompt": "첨부 문서를 읽으세요.", "tools": ["parse_doc"],
        })
        assert agent.status_code == 201
        response = await client.post("/api/chat/messages", headers=headers, json={
            "content": "규정.hwpx를 읽어줘", "agent_id": agent.json()["agent_id"],
        })
    assert response.status_code == 200
    event = response.json()["tool_events"][0]
    assert event["ok"] and event["tool_name"] == "parse_doc"
    parsed = json.loads(event["result"])
    assert parsed["tables"] == 1 and "제1장 총칙" in parsed["text"]
