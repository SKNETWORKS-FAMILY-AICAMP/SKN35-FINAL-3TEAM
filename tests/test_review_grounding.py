import json

import pytest

from contracts.harness import LLMToolCall, Message
from contracts.permission import Principal
from harness import create_harness
from harness.review_grounding import ReviewLookupGuard
from tests.fakes import FakeRegistry, FakeTool, FixedApprover, MemoryAudit, ScriptedChecker, ScriptedLLM

QUESTION = "REVIEW-001을 지금 실제로 조회해서 상태와 누락 서류를 알려줘."
DATA = {"case_id": "REVIEW-001", "status": "서류 검토 중", "submitted_docs": ["사업자등록증", "재무제표"],
        "missing_docs": ["납세증명서"], "review_request": None}
FAKE_ANSWER = "현재 처리 중이며 사업자등록증과 연체 이력 확인서가 누락되었습니다."


def lookup(case_id="REVIEW-001"):
    return {"tool_calls": [{"id": "lookup", "name": "get_review_case", "arguments": {"case_id": case_id}}]}


def build(script, *, payload=DATA, tools=True, decision="allow", max_turns=6, raises=None):
    llm = ScriptedLLM(script)
    audit, executed = MemoryAudit(), []
    runner = create_harness(
        llm=llm, tools=FakeRegistry([FakeTool("get_review_case", json.dumps(payload, ensure_ascii=False), log=executed, raises=raises)] if tools else []),
        checker=ScriptedChecker({"get_review_case": decision}), approver=FixedApprover(True),
        audit=audit, max_turns=max_turns, require_review_lookup=True,
    )
    return runner, llm, audit, executed


async def test_fabricated_answer_is_discarded_retry_is_read_only_and_actual_data_is_used():
    runner, llm, audit, executed = build([{"content": FAKE_ANSWER}, lookup(), {"content": FAKE_ANSWER}])
    result = await runner.run(QUESTION, Principal("u"), "s", agent_id="agent", run_id="run")
    assert "서류 검토 중" in result.final_message.content and "납세증명서" in result.final_message.content
    assert "연체 이력" not in result.final_message.content
    assert all(FAKE_ANSWER not in message.content for message in result.messages)
    assert executed == ["get_review_case"] and result.llm_calls == 2
    assert [spec.name for spec in llm.calls[1][1]] == ["get_review_case"]
    assert "심사 조회 확인" in llm.calls[1][0][-1].content
    assert not any("심사 조회 확인" in message.content for message in result.messages)
    assert audit.records[0].call.agent_id == "agent" and audit.records[0].call.run_id == "run"


async def test_second_plain_answer_falls_back_to_real_read_without_saving_fabricated_data():
    runner, llm, audit, executed = build([{"content": FAKE_ANSWER}] * 3)
    result = await runner.run(QUESTION, Principal("u"), "s")
    assert "납세증명서" in result.final_message.content
    assert all(FAKE_ANSWER not in message.content for message in result.messages)
    assert len(llm.calls) == 2 and executed == ["get_review_case"]
    assert audit.records[0].executed is True


async def test_retry_cannot_turn_into_a_write():
    runner, _, audit, executed = build([
        {"content": FAKE_ANSWER},
        {"tool_calls": [{"id": "write", "name": "submit_review_request", "arguments": {"case_id": "REVIEW-001"}}]},
        {"content": FAKE_ANSWER},
    ])
    result = await runner.run(QUESTION, Principal("u"), "s")
    assert "납세증명서" in result.final_message.content
    assert executed == ["get_review_case"] and len(audit.records) == 1
    assert all(call.tool_name == "get_review_case" for message in result.messages for call in message.tool_calls)


async def test_no_lookup_tool_returns_unavailable_without_calling_model():
    runner, llm, _, _ = build([], tools=False)
    result = await runner.run(QUESTION, Principal("u"), "s")
    assert "조회 도구가 없어" in result.final_message.content and not llm.calls


async def test_denied_lookup_stops_without_retry_or_bypass():
    runner, llm, audit, executed = build([lookup()], decision="deny")
    result = await runner.run(QUESTION, Principal("u"), "s")
    assert result.stop_reason == "denied" and len(llm.calls) == 1
    assert not executed and audit.records[0].executed is False


@pytest.mark.parametrize("payload", [{**DATA, "case_id": "REVIEW-999"}, {"case_id": "REVIEW-001"}])
async def test_wrong_case_or_incomplete_payload_does_not_count_as_verified(payload):
    runner, _, _, _ = build([lookup(), {"content": FAKE_ANSWER}, {"content": FAKE_ANSWER}, {"content": FAKE_ANSWER}], payload=payload)
    result = await runner.run(QUESTION, Principal("u"), "s")
    assert "조회하지 못해" in result.final_message.content


async def test_low_turn_budget_never_accepts_unverified_answer():
    runner, llm, _, _ = build([{"content": FAKE_ANSWER}], max_turns=1)
    result = await runner.run(QUESTION, Principal("u"), "s")
    assert "조회하지 못해" in result.final_message.content and len(llm.calls) == 1


async def test_failed_lookup_cannot_be_used_as_a_source():
    runner, _, audit, executed = build([lookup(), {"content": FAKE_ANSWER}, lookup(), {"content": FAKE_ANSWER}], raises="lookup failed")
    result = await runner.run(QUESTION, Principal("u"), "s")
    assert "조회하지 못해" in result.final_message.content
    assert "연체 이력" not in result.final_message.content
    assert len(executed) == 2 and all(record.tool_ok is False for record in audit.records)


async def test_generic_explanation_is_not_forced_into_record_lookup():
    runner, llm, _, executed = build([{"content": "심사는 제출 서류를 검토하는 과정입니다."}])
    result = await runner.run("심사 절차가 뭐야?", Principal("u"), "s")
    assert result.final_message.content.startswith("심사는") and len(llm.calls) == 1 and not executed
    assert ReviewLookupGuard.from_request("REVIEW-001 조회 코드 예시를 작성해줘", []) is None
    assert ReviewLookupGuard.from_request("REVIEW-001을 조회하고 메모를 샘플로 해서 등록해줘", []) is not None


async def test_followup_requeries_instead_of_trusting_previous_result():
    history = [Message("assistant", "", tool_calls=(LLMToolCall("previous", "get_review_case", {"case_id": "REVIEW-001"}),)),
               Message("tool", json.dumps(DATA), tool_call_id="previous")]
    runner, _, _, executed = build([{"content": FAKE_ANSWER}, lookup(), {"content": FAKE_ANSWER}])
    result = await runner.run("다시 조회해서 누락 서류를 알려줘", Principal("u"), "s", history)
    assert executed == ["get_review_case"] and "납세증명서" in result.final_message.content


def test_all_requested_cases_must_be_verified_and_unexecuted_registration_is_not_claimed():
    guard = ReviewLookupGuard.from_request("REVIEW-001과 REVIEW-002 조회 후 등록해줘", [])
    guard.record("get_review_case", {"case_id": "REVIEW-001"}, json.dumps(DATA))
    assert guard.missing == ("REVIEW-002",)
    guard.record("get_review_case", {"case_id": "REVIEW-002"}, json.dumps({**DATA, "case_id": "REVIEW-002"}))
    assert not guard.missing and "등록은 실행되지 않았습니다" in guard.summary()
    guard.record("submit_review_request", {"case_id": "REVIEW-001"}, json.dumps({"case_id": "REVIEW-001", "status": "registered", "notes": "메모", "request_id": "REQ-1"}))
    assert "REQ-1" in guard.summary() and "메모" in guard.summary()


@pytest.mark.parametrize("approved", [True, False])
async def test_lookup_guard_preserves_real_submission_approval_and_storage(tmp_path, approved):
    from pathlib import Path
    from permission import create_checker
    from permission.policy import load_policy
    from tools.get_review_case import GetReviewCaseTool
    from tools.registry import SimpleToolRegistry
    from tools.review_store import ReviewStore
    from tools.submit_review_request import SubmitReviewRequestTool

    root = Path(__file__).resolve().parents[1]
    store = ReviewStore(root / "data/fixtures/review_cases.json", tmp_path / "requests.sqlite3")
    rules, default = load_policy(root / "config/permissions.yaml")
    llm = ScriptedLLM([lookup(), {"tool_calls": [{"id": "submit", "name": "submit_review_request", "arguments": {"case_id": "REVIEW-001", "notes": "승인 검증"}}]}, {"content": FAKE_ANSWER}])
    audit, approver = MemoryAudit(), FixedApprover(approved)
    runner = create_harness(
        llm=llm, tools=SimpleToolRegistry([GetReviewCaseTool(store), SubmitReviewRequestTool(store)]),
        checker=create_checker(rules=rules, default=default), approver=approver, audit=audit,
        max_turns=6, require_review_lookup=True,
    )
    kim = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
    result = await runner.run("REVIEW-001을 조회 후 등록해줘", kim, "s")
    assert approver.requests == ["submit"]
    assert bool(store.get_case("REVIEW-001", kim)["review_request"]) == approved
    assert audit.records[-1].executed == approved
    assert "납세증명서" in result.final_message.content and "연체 이력" not in result.final_message.content
    if approved:
        assert result.stop_reason == "completed" and "심사 요청: 등록됨" in result.final_message.content
        assert "승인 검증" in result.final_message.content
    else:
        assert result.stop_reason == "denied" and result.denial.kind == "rejected"
