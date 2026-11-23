"""문서 질문이면 검색 강제 (harness/grounding.py)."""

from __future__ import annotations

from contracts.permission import Principal
from harness import create_harness
from harness.grounding import RequireSearch, keyword_query
from tests.fakes import FakeRegistry, FakeTool, FixedApprover, MemoryAudit, ScriptedChecker, ScriptedLLM

KIM = Principal("kim")
FOUND = '{"query": "서류 반려", "matches": [{"title": "공통 지침", "location": "제2절 1항", "preview": "..."}]}'


def build(script, *, tools=("search_doc", "today"), max_turns=5, require=True, checker=None):
    llm = ScriptedLLM(script)
    log: list[str] = []
    audit = MemoryAudit()
    harness = create_harness(
        llm=llm, tools=FakeRegistry([FakeTool(n, output=FOUND if n == "search_doc" else "ok", log=log) for n in tools]),
        checker=checker or ScriptedChecker({}), approver=FixedApprover(True), audit=audit, max_turns=max_turns,
        require_search=RequireSearch() if require else None,
    )
    return harness, llm, log, audit


async def test_ungrounded_citation_is_dropped_and_search_is_forced():
    harness, llm, log, audit = build([
        {"content": "지점장이 승인합니다.\n근거: 공통 지침 제5절 3항"},  # 검색 없이 지어낸 답
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "예외 승인"}}]},  # 검색만 허용된 호출
        {"content": "찾지 못했습니다."},
    ])
    result = await harness.run("서류 반려 예외는 누가 승인해?", KIM, "s")
    assert result.final_message.content == "찾지 못했습니다." and result.llm_calls == 3
    assert log == ["search_doc"] and len(audit.records) == 1  # 강제 검색도 권한 검사·감사 로그를 거친다
    assert all("제5절 3항" not in m.content for m in result.messages)  # 지어낸 답은 기록에 남지 않는다
    messages, tools = llm.calls[1]
    assert [t.name for t in tools] == ["search_doc"] and "먼저 사내 문서 검색" in messages[-1].content
    assert all("먼저 사내 문서 검색" not in m.content for m in result.messages)  # 안내 문구도 기록에 없음


async def test_harness_searches_itself_when_llm_still_refuses():
    harness, _, log, _ = build([
        {"content": "근거: 공통 지침 제5절"},
        {"content": "검색 안 할래요"},  # 검색 툴만 줬는데도 안 부름
        {"content": "공통 지침에 따르면..."},
    ])
    result = await harness.run("서류 반려 사유는 어떻게 적어야 해?", KIM, "s")
    assert log == ["search_doc"]
    forced = next(m for m in result.messages if m.tool_calls)
    assert forced.tool_calls[0].call_id.startswith("forced_")
    assert forced.tool_calls[0].arguments == {"query": "서류 반려"}


async def test_no_force_for_non_doc_answers_or_when_already_searched():
    harness, llm, log, _ = build([{"content": "안녕하세요!"}])
    assert (await harness.run("안녕", KIM, "s")).final_message.content == "안녕하세요!" and len(llm.calls) == 1

    harness, llm, _, _ = build([
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "반려"}}]},
        {"content": "누락 항목을 적습니다.\n근거: 공통 지침 제2절 1항"},
    ])
    result = await harness.run("서류 반려 규정 알려줘", KIM, "s")
    assert len(llm.calls) == 2 and result.stop_reason == "completed"


async def test_no_force_without_search_tool_or_when_disabled_or_no_turns_left():
    for kwargs in ({"tools": ("today",)}, {"require": False}, {"max_turns": 2}):
        harness, llm, log, _ = build([{"content": "근거: 공통 지침 제2절"}], **kwargs)
        result = await harness.run("반려 규정 알려줘", KIM, "s")
        assert result.final_message.content == "근거: 공통 지침 제2절" and log == [], kwargs


async def test_forced_only_once_per_run():
    harness, llm, log, _ = build([
        {"content": "근거: 지어낸 지침"},
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "x"}}]},
        {"content": "근거: 또 지어낸 지침"},  # 검색 결과를 무시해도 두 번 강제하지 않는다
    ], tools=("search_doc",))
    result = await harness.run("규정 알려줘", KIM, "s")
    assert result.final_message.content == "근거: 또 지어낸 지침" and log == ["search_doc"]


async def test_denied_forced_search_ends_like_any_denial():
    harness, _, log, _ = build([{"content": "근거: 지침"}, {"content": ""}],
                               checker=ScriptedChecker({"search_doc": "deny"}))
    result = await harness.run("규정 알려줘", KIM, "s")
    assert result.stop_reason == "denied" and log == []


def test_trigger_and_keyword_query():
    rs = RequireSearch()
    assert rs.needed("안녕", "근거: 지침 제2절")
    assert rs.needed("예외는 누가 승인해?", "지점장입니다")
    assert rs.needed("반려 사유는 어떻게 적어야 해?", "구체적으로 씁니다")
    assert not rs.needed("REVIEW-001 상태랑 빠진 서류 알려줘", "서류 검토 중입니다")
    assert not rs.needed("3 곱하기 4", "12입니다")
    assert keyword_query("서류 반려 건 예외 처리는 누가 승인해?") == "서류 반려"
    assert keyword_query("내규에서 휴가 규정은?") == "내규 휴가"


async def test_boilerplate_citation_after_other_tool_is_not_forced():
    # 심사 조회처럼 문서가 아닌 툴로 답한 뒤 붙인 근거 줄 때문에 쓸데없이 검색하지 않는다
    harness, llm, log, _ = build([
        {"tool_calls": [{"id": "t1", "name": "today", "arguments": {}}]},
        {"content": "11월 26일입니다.\n근거: 날짜 계산 결과"},
    ])
    await harness.run("3영업일 뒤가 언제야?", KIM, "s")
    assert log == ["today"] and len(llm.calls) == 2


async def test_parsed_attachment_is_grounded_without_forcing_internal_search():
    harness, llm, log, _ = build([
        {"tool_calls": [{"id": "p1", "name": "parse_doc", "arguments": {"file_id": "file.hwpx"}}]},
        {"content": "첨부 규정의 내용입니다.\n근거: 업무지침.hwpx 제1장"},
    ], tools=("search_doc", "parse_doc"))
    result = await harness.run("첨부한 규정 요약해줘", KIM, "s")
    assert result.stop_reason == "completed"
    assert log == ["parse_doc"] and len(llm.calls) == 2


def test_empty_citation_lines_are_removed():
    from harness.grounding import strip_empty_citation

    assert strip_empty_citation("납세증명서가 빠졌습니다.\n근거: 검색 결과 없음") == "납세증명서가 빠졌습니다."
    assert strip_empty_citation("답\n근거: 관련 문서 없음\n") == "답"
    kept = "답\n근거: 공통 지침 제2절 1항"
    assert strip_empty_citation(kept) == kept
    assert not RequireSearch().needed("안녕", "안녕하세요\n근거: 관련 문서 없음")


async def test_forced_search_uses_required_tool_when_llm_supports_it():
    class RequiredToolLLM(ScriptedLLM):
        def __init__(self, script):
            super().__init__(script)
            self.required: list[str] = []

        async def chat_required_tool(self, messages, tool):
            self.required.append(tool.name)
            return await self.chat(messages, [tool])

    llm = RequiredToolLLM([
        {"content": "근거: 지어낸 지침 제5절"},
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "반려"}}]},
        {"content": "찾지 못했습니다."},
    ])
    harness = create_harness(
        llm=llm, tools=FakeRegistry([FakeTool("search_doc", output=FOUND)]), checker=ScriptedChecker({}),
        approver=FixedApprover(True), audit=MemoryAudit(), max_turns=5, require_search=RequireSearch(),
    )
    result = await harness.run("반려 규정 알려줘", KIM, "s")
    assert llm.required == ["search_doc"] and result.final_message.content == "찾지 못했습니다."
