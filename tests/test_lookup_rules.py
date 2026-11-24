"""조회 규칙 목록(harness/grounding.py)과 최종 답 검사(harness/answer_check.py)."""

from __future__ import annotations

import json

import pytest

from contracts.harness import ToolSpec
from contracts.permission import Principal
from harness import create_harness
from harness.answer_check import check_answer, drop_sentences_with, false_completion, unknown_answer_numbers
from harness.grounding import DEFAULT_RULES, LoanLookup, MarketLookup, RequireSearch
from tests.fakes import FakeRegistry, FakeTool, FixedApprover, MemoryAudit, ScriptedChecker, ScriptedLLM

KIM = Principal("E1001")
LOANS = json.dumps({"as_of": "2026-11-23", "total": 6, "counts": {"만기 경과": 1, "7일 이내": 2, "8~30일": 3},
                    "items": [{"customer": "홍*동", "maturity_date": "2026-11-18"}]}, ensure_ascii=False)
EMPTY = '{"query": "휴가", "matches": []}'
FOUND = '{"query": "휴가", "matches": [{"title": "인사규정", "location": "제12조(연차)", "preview": "연차는 15일"}]}'


@pytest.fixture(autouse=True)
def answer_log(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSWER_CHECK_LOG_PATH", str(tmp_path / "answer_checks.jsonl"))
    return tmp_path / "answer_checks.jsonl"


def tool(name, output="ok", effect="read", log=None):
    fake = FakeTool(name, output=output, log=log)
    fake.spec = ToolSpec(name=name, description=f"fake {name}", effect=effect)
    return fake


def harness(script, tools, max_turns=6):
    llm = ScriptedLLM(script)
    return create_harness(llm=llm, tools=FakeRegistry(tools), checker=ScriptedChecker({}), approver=FixedApprover(True),
                          audit=MemoryAudit(), max_turns=max_turns, lookup_rules=DEFAULT_RULES), llm


def test_rules_pick_the_right_lookup():
    loans, market, docs = LoanLookup(), MarketLookup(), RequireSearch()
    assert loans.needed("7일 안에 만기되는 대출 몇 건이야?", "") and loans.arguments("7일 안에 만기 대출") == {"days": 7}
    assert loans.arguments("만기 대출 알려줘") == {"days": 30} and loans.arguments("200일 만기 대출") == {"days": 90}
    assert not loans.needed("만기 연장 규정은 어떻게 돼?", "")  # 규정 질문은 문서 검색 규칙
    assert market.needed("오늘 원달러 환율 얼마야?", "") and not market.needed("기준금리란 뭐야?", "")
    assert market.arguments("2026-11-18 환율") == {"base_date": "2026-11-18"}
    assert docs.needed("연차 규정 알려줘", "") and docs.retry_arguments("연차 휴가 규정 알려줘", {"연차 휴가"}) == {"query": "연차"}


async def test_loan_answer_without_lookup_is_dropped_and_lookup_forced():
    log = []
    runner, llm = harness([
        {"content": "만기 대출은 12건입니다."},  # 조회 없이 지어낸 답
        {"content": "조회 안 할래요"},  # 조회 툴만 줬는데도 안 부름 → 하네스가 직접 부른다
        {"content": "30일 안에 만기되는 대출은 6건입니다. 만기 경과 1건(홍*동, 11.18)을 먼저 보세요."},
    ], [tool("loans_maturing", LOANS, log=log), tool("send_maturity_alert", effect="write")])
    result = await runner.run("만기 대출 몇 건이야?", KIM, "s")
    assert log == ["loans_maturing"] and "6건" in result.final_message.content
    forced = next(m for m in result.messages if m.tool_calls)
    assert forced.tool_calls[0].arguments == {"days": 30}
    assert all("12건" not in m.content for m in result.messages)
    assert [t.name for t in llm.calls[1][1]] == ["loans_maturing"]


async def test_wrong_number_after_lookup_is_rewritten_then_dropped(answer_log):
    runner, llm = harness([
        {"tool_calls": [{"id": "l1", "name": "loans_maturing", "arguments": {"days": 30}}]},
        {"content": "총 47건입니다. 홍*동 고객을 먼저 보세요."},
        {"content": "총 47건입니다. 홍*동 고객을 먼저 보세요."},  # 다시 써도 같은 숫자
    ], [tool("loans_maturing", LOANS)])
    result = await runner.run("만기 대출 정리해줘", KIM, "s")
    assert result.final_message.content == "홍*동 고객을 먼저 보세요."  # 틀린 숫자가 든 문장만 뺀다
    assert "조회 결과에 없는 숫자: 47" in llm.calls[2][0][-1].content
    logged = [json.loads(line) for line in answer_log.read_text(encoding="utf-8").splitlines()]
    assert len(logged) == 2 and logged[0]["problems"] == ["조회 결과에 없는 숫자: 47"]


async def test_false_send_claim_gets_a_second_chance_to_really_send():
    log = []
    runner, llm = harness([
        {"tool_calls": [{"id": "p1", "name": "prepare_maturity_notices", "arguments": {"days": 7}}]},
        {"content": "L-1002 고객에게 메일을 보냈습니다."},  # 보내지 않고 보냈다고 함
        {"tool_calls": [{"id": "s1", "name": "send_maturity_notice", "arguments": {"loan_id": "L-1002"}}]},
        {"content": "L-1002 고객에게 메일을 보냈습니다."},
    ], [tool("prepare_maturity_notices", LOANS, log=log), tool("send_maturity_notice", '{"sent": true}', "send", log)])
    result = await runner.run("L-1002 보내줘", KIM, "s")
    assert log == ["prepare_maturity_notices", "send_maturity_notice"]
    assert result.final_message.content == "L-1002 고객에게 메일을 보냈습니다."
    assert "실제로 실행하지 않은 작업" in llm.calls[2][0][-1].content


async def test_false_send_claim_twice_becomes_fixed_text():
    runner, _ = harness([{"content": "메일을 보냈습니다."}, {"content": "메일을 보냈습니다."}],
                        [tool("send_maturity_notice", effect="send")])
    result = await runner.run("L-1002 보내줘", KIM, "s")
    assert result.final_message.content.startswith("요청한 작업을 실제로 실행하지 못했습니다")


async def test_empty_search_is_retried_once_then_fixed_text():
    log = []
    runner, _ = harness([
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "연차 휴가"}}]},
        {"content": "규정에 없습니다."},
        {"content": "규정에 없습니다."},
    ], [tool("search_doc", EMPTY, log=log)])
    result = await runner.run("연차 휴가 규정 알려줘", KIM, "s")
    assert log == ["search_doc", "search_doc"]
    research = [m for m in result.messages if m.tool_calls][-1].tool_calls[0]
    assert research.call_id.startswith("research_") and research.arguments == {"query": "연차"}
    assert result.final_message.content == "등록된 규정에서 근거를 찾지 못했습니다."


async def test_made_up_article_is_not_shown():
    runner, _ = harness([
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "연차"}}]},
        {"content": "연차는 15일입니다.\n근거: 인사규정 제99조"},
        {"content": "연차는 15일입니다.\n근거: 인사규정 제99조"},
    ], [tool("search_doc", FOUND)])
    result = await runner.run("연차 규정 알려줘", KIM, "s")
    assert result.final_message.content == "등록된 규정에서 근거를 찾지 못했습니다."


async def test_grounded_answer_passes_untouched():
    runner, llm = harness([
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "연차"}}]},
        {"content": "1. 연차는 15일입니다. [1]\n근거: 인사규정 제12조"},
    ], [tool("search_doc", FOUND)])
    result = await runner.run("연차 규정 알려줘", KIM, "s")
    assert result.final_message.content.startswith("1. 연차는 15일") and len(llm.calls) == 2


def test_answer_check_pieces():
    assert unknown_answer_numbers("1. 홍*동(11.18) 6건 [2]", [LOANS]) == set()
    assert unknown_answer_numbers("12월 3일 만기", [LOANS]) == {"12"}
    assert drop_sentences_with("6건입니다. 47건이 급합니다.\n확인하세요.", {"47"}) == "6건입니다. 확인하세요."
    assert false_completion("알림함으로 보냈습니다.", False) and not false_completion("알림함으로 보냈습니다.", True)
    assert not false_completion("메일은 보내지 않았습니다.", False) and not false_completion("회의록을 정리했습니다.", False)
    assert check_answer("만기 12건", sources=[LOANS], grounded=False, wrote=False).ok  # 조회 안 한 답은 숫자를 보지 않는다


async def test_notice_agent_is_forced_to_prepare_when_it_only_talks():
    """UC-05 에이전트에는 loans_maturing 이 없다. 말로만 답하면 안내문 준비 툴로 조회를 강제한다 (실측 실패 사례)."""
    log = []
    runner, llm = harness([
        {"content": "7일 안에 만기되는 고객의 안내 메일을 준비해드릴게요. 이대로 보낼까요?"},
        {"tool_calls": [{"id": "p1", "name": "prepare_maturity_notices", "arguments": {"days": 7}}]},
        {"content": "안내 메일 3건을 준비했습니다. 이대로 보낼까요?"},
    ], [tool("prepare_maturity_notices", LOANS, log=log), tool("send_maturity_notice", effect="send")])
    result = await runner.run("7일 안에 만기되는 고객 안내 메일 준비해줘", KIM, "s")
    assert log == ["prepare_maturity_notices"] and result.final_message.content.startswith("안내 메일 3건")
    assert [t.name for t in llm.calls[1][1]] == ["prepare_maturity_notices"]
    assert "prepare_maturity_notices 툴만" in llm.calls[1][0][-1].content
