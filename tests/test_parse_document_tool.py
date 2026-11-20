"""업로드 문서 읽기 툴(parse_document). 모델 없이 도는 HWPX·거절 경로만 쓴다."""

from __future__ import annotations

import json

import pytest

from contracts.permission import Principal, ToolCall
from tests.knowledge.ingestion.parsers.test_pipeline import _hwpx  # 제목 1 + 본문 1 + 1x2 표인 가장 작은 HWPX
from tools import parse_document as pd
from tools.parse_document import ParseDocumentTool

KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})


def call(args: dict) -> ToolCall:
    return ToolCall("c1", "parse_document", args, KIM, "s1")


@pytest.fixture
def uploads(tmp_path):
    (tmp_path / "규정.hwpx").write_bytes(_hwpx())
    (tmp_path / "이상한.bin").write_bytes(b"\x00\x01\x02 not a document")
    return tmp_path


async def test_hwpx_parsed_then_read_from_cache(uploads):
    tool = ParseDocumentTool(uploads)
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
    r = await ParseDocumentTool(uploads).run(call({"file_id": file_id}))
    assert not r.ok and "찾을 수 없습니다" in r.content  # 폴더 밖 경로는 있는지도 알려주지 않는다


async def test_rejected_document_reports_reason(uploads):
    r = await ParseDocumentTool(uploads).run(call({"file_id": "이상한.bin"}))
    assert not r.ok and r.content.startswith("문서를 읽지 못했습니다") and r.data["status"] == "rejected"
    assert not (uploads / "parsed" / "이상한.bin.json").exists()  # 실패 결과는 캐시하지 않는다


async def test_long_text_is_truncated(uploads, monkeypatch):
    monkeypatch.setattr(pd, "MAX_CHARS", 10)
    body = json.loads((await ParseDocumentTool(uploads).run(call({"file_id": "규정.hwpx"}))).content)
    assert body["truncated"] is True and len(body["text"]) == 10


@pytest.mark.parametrize("pages", [[0, 1], [3, 2], [1], "1-2"])
async def test_bad_pages_argument_is_error(uploads, pages):
    r = await ParseDocumentTool(uploads).run(call({"file_id": "규정.hwpx", "pages": pages}))
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
