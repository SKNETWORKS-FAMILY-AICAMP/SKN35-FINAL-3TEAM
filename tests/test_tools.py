"""업무 툴: 문서 검색·열람 권한 필터(SYS-03), 날짜."""

from __future__ import annotations

import json
import re
from pathlib import Path

from contracts.permission import Principal, ToolCall
from tools.read_doc import ReadDocTool
from tools.search_doc import SearchDocsTool
from tools.today import TodayTool

DOCS = Path(__file__).parent.parent / "data/fixtures/doc_chunks.json"

KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
LEE = Principal("lee", ("dept_head",), {"dept_id": 10, "security_grade": 3})
PARK = Principal("park", ("member",), {"dept_id": 20, "security_grade": 2})
NOBODY = Principal("x", ("member",), {})  # 속성이 없으면 아무것도 못 읽는다


def call(tool: str, args: dict, who: Principal) -> ToolCall:
    return ToolCall("c1", tool, args, who, "s1")


async def search(who: Principal, query: str = "서류 반려") -> list[str]:
    result = await SearchDocsTool(DOCS).run(call("search_doc", {"query": query}, who))
    assert result.ok
    return [m["doc_id"] for m in json.loads(result.content)["matches"]]


async def test_search_filters_by_dept_and_grade():
    assert await search(KIM) == ["doc-001", "doc-002"]  # 보안등급 3 문서는 안 보임
    assert await search(LEE) == ["doc-001", "doc-002", "doc-003"]
    assert await search(PARK) == ["doc-001"]  # 다른 부서 문서는 안 보임
    assert await search(NOBODY) == []


async def test_read_applies_same_filter():
    tool = ReadDocTool(DOCS)
    ok = await tool.run(call("get_doc_chunk", {"chunk_id": "doc-002-01"}, KIM))
    assert ok.ok and "누락된 항목" in json.loads(ok.content)["text"]
    for who, chunk in ((PARK, "doc-002-01"), (KIM, "doc-003-01")):
        r = await tool.run(call("get_doc_chunk", {"chunk_id": chunk}, who))
        assert not r.ok and r.content == "문단을 찾을 수 없습니다."  # 있는지도 알려주지 않는다


async def test_bad_arguments_are_errors_not_crashes():
    assert not (await SearchDocsTool(DOCS).run(call("search_doc", {"query": "  "}, KIM))).ok
    assert not (await SearchDocsTool(DOCS).run(call("search_doc", {}, KIM))).ok
    assert not (await ReadDocTool(DOCS).run(call("get_doc_chunk", {"chunk_id": 3}, KIM))).ok
    missing = await SearchDocsTool(Path("/nope.json")).run(call("search_doc", {"query": "a"}, KIM))
    assert not missing.ok


async def test_today_returns_korean_date():
    r = await TodayTool(Path(__file__).parent.parent / "data/fixtures/business_holidays_demo.json").run(call("today", {}, KIM))
    data = json.loads(r.content)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"]) and data["weekday"].endswith("요일")


def test_all_tools_registered_with_display_names():
    from services.container import build_tools

    tools = build_tools(DOCS)
    assert {t.spec.name for t in tools} >= {"search_doc", "get_doc_chunk", "today", "add", "get_review_case", "submit_review_request"}
    assert all(t.spec.display_name for t in tools)
    specs = {t.spec.name: t.spec for t in tools}
    assert (specs["parse_doc"].effect, specs["parse_doc"].domain) == ("read", "문서")
    for name in ("search_mail", "read_mail", "send_mail"):
        if name in specs:
            assert specs[name].effect == ("send" if name == "send_mail" else "read")
            assert specs[name].domain == "메일"


async def test_today_counts_business_days_skipping_weekend_and_holiday():
    tool = TodayTool(Path(__file__).parent.parent / "data/fixtures/business_holidays_demo.json")
    # 2026-11-20(금) + 1영업일: 토·일 건너뛰고 11-23(월)은 테스트 휴일이라 11-24(화)
    r = await tool.run(call("today", {"base_date": "2026-11-20", "days": 1, "mode": "business"}, KIM))
    data = json.loads(r.content)
    assert (data["target_date"], data["target_weekday"]) == ("2026-11-24", "화요일")
    r = await tool.run(call("today", {"base_date": "2026-11-20", "days": 3}, KIM))  # 달력일
    assert json.loads(r.content)["target_date"] == "2026-11-23"
    bad = await tool.run(call("today", {"days": -1, "mode": "business"}, KIM))
    assert not bad.ok
