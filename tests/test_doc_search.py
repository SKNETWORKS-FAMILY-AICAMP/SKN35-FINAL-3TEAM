import json

import pytest

from contracts.permission import Principal, ToolCall
from tools.search_doc import SearchDocsTool


@pytest.mark.parametrize("length", [0, 199, 200, 201])
async def test_search_preview_and_access_filter(tmp_path, length):
    text = "가" * length
    public = {
        "doc_id": "public",
        "chunk_id": "public-1",
        "title": "서류 반려",
        "location": "1항",
        "text": text,
        "scope": "company",
        "dept_id": None,
        "security_grade": 1,
    }
    restricted = [
        {**public, "doc_id": "other-dept", "scope": "dept", "dept_id": 20, "text": "부서 제한 원문"},
        {**public, "doc_id": "higher-grade", "security_grade": 3, "text": "등급 제한 원문"},
    ]
    path = tmp_path / "chunks.json"
    path.write_text(json.dumps([public, *restricted], ensure_ascii=False), encoding="utf-8")
    principal = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
    call = ToolCall("search", "search_doc", {"query": "서류 반려"}, principal, "session")

    result = await SearchDocsTool(path).run(call)

    assert result.ok
    payload = json.loads(result.content)
    assert payload == result.data
    assert payload["matches"] == [{
        "doc_id": "public",
        "chunk_id": "public-1",
        "title": "서류 반려",
        "location": "1항",
        "preview": text[:200],
        "is_truncated": length > 200,
    }]
    assert "부서 제한 원문" not in result.content
    assert "등급 제한 원문" not in result.content
