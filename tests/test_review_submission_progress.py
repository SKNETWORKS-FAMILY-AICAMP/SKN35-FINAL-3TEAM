import json
import sqlite3
from pathlib import Path

import pytest

from contracts.permission import Principal
from contracts.harness import LLMToolCall, Message
from harness import create_harness
from harness.review_grounding import ReviewLookupGuard
from permission import create_checker
from permission.policy import load_policy
from tests.fakes import FixedApprover, MemoryAudit, ScriptedLLM
from tests.test_review_grounding import lookup
from tools.get_review_case import GetReviewCaseTool
from tools.registry import SimpleToolRegistry
from tools.review_store import ReviewStore
from tools.submit_review_request import SubmitReviewRequestTool

ROOT = Path(__file__).resolve().parents[1]
KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
NOTES = "누락 서류 보완 여부 확인 요청"
QUESTION = f"REVIEW-001을 메모 '{NOTES}'로 다시 등록해줘. 기존 요청과 동일한 조건으로 진행해줘."


def submit(call_id="submit"):
    return {"id": call_id, "name": "submit_review_request", "arguments": {"case_id": "REVIEW-001", "notes": NOTES}}


def build(tmp_path, script, *, existing=False, approved=True, submission=True, fails_after_commit=False, max_turns=6):
    store = ReviewStore(ROOT / "data/fixtures/review_cases.json", tmp_path / "requests.sqlite3")
    existing_id = store.submit("REVIEW-001", NOTES, KIM)["request_id"] if existing else None
    llm, audit, approver = ScriptedLLM(script), MemoryAudit(), FixedApprover(approved)
    rules, default = load_policy(ROOT / "config/permissions.yaml")

    class LostSubmissionResponse(SubmitReviewRequestTool):
        async def run(self, call):
            await super().run(call)
            raise RuntimeError("response lost after commit")

    writer = LostSubmissionResponse(store) if fails_after_commit else SubmitReviewRequestTool(store)
    runner = create_harness(
        llm=llm, tools=SimpleToolRegistry([GetReviewCaseTool(store), *([writer] if submission else [])]),
        checker=create_checker(rules=rules, default=default), approver=approver, audit=audit,
        max_turns=max_turns, require_review_lookup=True,
    )
    return runner, store, llm, audit, approver, existing_id


@pytest.mark.parametrize("existing", [False, True])
async def test_lookup_then_registration_stage_is_requested_and_approved(tmp_path, existing):
    runner, store, llm, audit, approver, existing_id = build(tmp_path, [lookup(), {"tool_calls": [submit()]}], existing=existing)
    result = await runner.run(QUESTION, KIM, "s")
    assert result.stop_reason == "completed"
    assert approver.requests == ["submit"] and len(llm.calls) == 2
    assert [spec.name for spec in llm.calls[1][1]] == ["submit_review_request"]
    assert "심사 등록 진행" in llm.calls[1][0][-1].content
    assert all("심사 등록 진행" not in message.content for message in result.messages)
    assert [record.executed for record in audit.records] == [True, True]
    request = store.get_case("REVIEW-001", KIM)["review_request"]
    assert request["notes"] == NOTES
    with sqlite3.connect(store.request_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_requests").fetchone()[0] == 1
    if existing:
        assert request["request_id"] == existing_id
        payload = json.loads(next(message.content for message in result.messages if message.tool_call_id == "submit" and message.role == "tool"))
        assert payload["already_registered"] is True and "중복 생성하지 않았습니다" in result.final_message.content


async def test_registration_retry_does_not_invent_arguments_if_model_still_refuses(tmp_path):
    runner, store, llm, audit, approver, _ = build(tmp_path, [lookup(), {"content": "심사 요청이 등록되었습니다."}])
    result = await runner.run("REVIEW-001을 등록해줘", KIM, "s")
    assert "등록은 실행되지 않았습니다" in result.final_message.content
    assert not any("등록되었습니다" in message.content for message in result.messages)
    assert len(llm.calls) == 2 and not approver.requests and len(audit.records) == 1
    assert store.get_case("REVIEW-001", KIM)["review_request"] is None


@pytest.mark.parametrize("existing,reuse", [(False, False), (True, False), (True, True)])
async def test_explicit_notes_or_same_conditions_reach_approval_even_if_model_returns_only_text(tmp_path, existing, reuse):
    runner, store, llm, audit, approver, original_id = build(tmp_path, [lookup(), {"content": "이미 등록되어 실행할 수 없습니다."}], existing=existing)
    question = "REVIEW-001을 기존과 동일한 조건으로 다시 등록해줘" if reuse else QUESTION
    result = await runner.run(question, KIM, "s")
    assert len(llm.calls) == 2 and len(approver.requests) == 1
    assert result.stop_reason == "completed" and audit.records[-1].approval == "approved"
    assert audit.records[-1].call.arguments == {"case_id": "REVIEW-001", "notes": NOTES}
    request = store.get_case("REVIEW-001", KIM)["review_request"]
    assert request["notes"] == NOTES
    if existing:
        assert request["request_id"] == original_id and "중복 생성하지 않았습니다" in result.final_message.content


def test_explicit_notes_override_model_invention_and_ambiguous_notes_have_no_fallback():
    guard = ReviewLookupGuard.from_request(QUESTION, [])
    guard.records["REVIEW-001"] = {"case_id": "REVIEW-001", "status": "서류 검토 중"}
    proposal = Message("assistant", "", tool_calls=(LLMToolCall(
        "write", "submit_review_request", {"case_id": "review-001 ", "notes": "모델이 만든 메모"}),))
    calls = guard.submission_calls(proposal, ("REVIEW-001",))
    assert calls[0].arguments == {"case_id": "REVIEW-001", "notes": NOTES}
    ambiguous = ReviewLookupGuard.from_request('REVIEW-001을 등록해줘. 메모 "가" 또는 메모 "나" 중 하나로 해줘.', [])
    ambiguous.records["REVIEW-001"] = guard.records["REVIEW-001"]
    assert ambiguous.submission_calls(Message("assistant", "설명만"), ("REVIEW-001",)) == ()
    invalid = ReviewLookupGuard.from_request(f'REVIEW-001을 메모 "{"가" * 1001}"로 기존과 동일하게 등록해줘', [])
    invalid.records["REVIEW-001"] = {"status": "서류 검토 중", "review_request": {"notes": NOTES}}
    assert invalid.trusted_submission_notes("REVIEW-001") is None


async def test_required_tool_capability_is_used_only_for_the_pending_registration(tmp_path):
    script = [lookup(), {"tool_calls": [submit()]}]
    runner, store, _, _, approver, _ = build(tmp_path, script)

    class RequiredModel(ScriptedLLM):
        def __init__(self):
            super().__init__(script)
            self.required = []

        async def chat_required_tool(self, messages, tool):
            self.required.append(tool.name)
            return await self.chat(messages, [tool])

    llm = RequiredModel()
    runner.llm = llm
    result = await runner.run(QUESTION, KIM, "s")
    assert result.stop_reason == "completed" and len(llm.calls) == 2
    assert llm.required == ["submit_review_request"]
    assert approver.requests == ["submit"]
    assert store.get_case("REVIEW-001", KIM)["review_request"] is not None


@pytest.mark.parametrize("approval", [False, "timeout"])
async def test_rejected_or_timed_out_registration_is_not_retried(tmp_path, approval):
    runner, store, llm, audit, approver, _ = build(tmp_path, [lookup(), {"tool_calls": [submit()]}], approved=approval)
    result = await runner.run(QUESTION, KIM, "s")
    assert result.stop_reason == "denied"
    assert result.denial.kind == ("rejected" if approval is False else "timeout")
    assert len(llm.calls) == 2 and approver.requests == ["submit"]
    assert audit.records[-1].executed is False
    assert store.get_case("REVIEW-001", KIM)["review_request"] is None


async def test_lost_write_response_is_not_automatically_retried(tmp_path):
    runner, store, llm, audit, approver, _ = build(tmp_path, [lookup(), {"tool_calls": [submit()]}], fails_after_commit=True)
    result = await runner.run(QUESTION, KIM, "s")
    assert "등록을 시도했지만 성공을 확인하지 못했습니다" in result.final_message.content
    assert "이번 요청에서 심사 요청 등록은 실행되지 않았습니다" not in result.final_message.content
    assert len(llm.calls) == 2 and approver.requests == ["submit"]
    assert audit.records[-1].executed is True and audit.records[-1].tool_ok is False
    assert store.get_case("REVIEW-001", KIM)["review_request"] is not None


async def test_duplicate_calls_in_same_reply_execute_registration_only_once(tmp_path):
    runner, _, _, audit, approver, _ = build(tmp_path, [{"tool_calls": [lookup()["tool_calls"][0], submit(), submit("duplicate")]}])
    result = await runner.run(QUESTION, KIM, "s")
    assert approver.requests == ["submit"] and len(audit.records) == 2
    blocked = next(message for message in result.messages if message.role == "tool" and message.tool_call_id == "duplicate")
    assert "이미 시도했습니다" in blocked.content


async def test_model_skipping_lookup_is_redirected_to_lookup_before_registration(tmp_path):
    runner, store, llm, audit, approver, _ = build(tmp_path, [
        {"tool_calls": [submit("too-early")]}, lookup(), {"tool_calls": [submit("after-lookup")]},
    ])
    result = await runner.run(QUESTION, KIM, "s")
    assert result.stop_reason == "completed" and approver.requests == ["after-lookup"]
    assert [spec.name for spec in llm.calls[1][1]] == ["get_review_case"]
    assert [spec.name for spec in llm.calls[2][1]] == ["submit_review_request"]
    assert [record.call.tool_name for record in audit.records] == ["get_review_case", "submit_review_request"]
    assert store.get_case("REVIEW-001", KIM)["review_request"]["notes"] == NOTES


async def test_lookup_only_cannot_turn_into_registration(tmp_path):
    runner, store, _, audit, approver, _ = build(tmp_path, [{"tool_calls": [lookup()["tool_calls"][0], submit()]}])
    result = await runner.run("REVIEW-001 조회만 해줘. 등록하지 마.", KIM, "s")
    assert result.stop_reason == "completed" and not approver.requests and len(audit.records) == 1
    assert store.get_case("REVIEW-001", KIM)["review_request"] is None


@pytest.mark.parametrize("options", [{"submission": False}, {"max_turns": 2}])
async def test_unavailable_registration_or_turn_budget_never_claims_success(tmp_path, options):
    runner, store, llm, _, approver, _ = build(tmp_path, [lookup()], **options)
    result = await runner.run(QUESTION, KIM, "s")
    assert "등록은 실행되지 않았습니다" in result.final_message.content
    assert len(llm.calls) == 1 and not approver.requests
    assert store.get_case("REVIEW-001", KIM)["review_request"] is None


def test_submission_progress_requires_explicit_intent_and_successful_lookup():
    assert not ReviewLookupGuard.from_request("REVIEW-001을 등록하지 말고 조회해줘", []).registration_requested
    guard = ReviewLookupGuard.from_request(QUESTION, [])
    assert guard.pending_submissions() == ()
    guard.record("get_review_case", {"case_id": "REVIEW-001"}, json.dumps({"case_id": "REVIEW-001", "status": "서류 검토 중"}))
    assert guard.pending_submissions() == ("REVIEW-001",)
    guard.submission_attempted.add("REVIEW-001")
    assert guard.pending_submissions() == ()


@pytest.mark.parametrize("question", [
    "REVIEW-001 등록 요청 상태를 알려줘",
    "REVIEW-001에 등록된 요청과 메모를 확인해줘",
    "REVIEW-001을 등록하지 말고 조회해줘",
    "REVIEW-001은 등록 요청을 하지 마. 상태만 알려줘",
])
def test_existing_registration_questions_and_negative_requests_do_not_request_writes(question):
    guard = ReviewLookupGuard.from_request(question, [])
    assert guard is not None and not guard.registration_requested


async def test_new_registration_after_failed_old_notes_uses_only_current_context(tmp_path):
    runner, store, llm, _, approver, _ = build(tmp_path, [lookup(), {"tool_calls": [submit()]}], existing=True)
    history = [Message("system", "권한 검사와 승인을 유지하세요."),
               Message("user", "다른 메모로 등록해줘"),
               Message("assistant", "이전 요청은 메모가 달라 등록에 실패했습니다.")]
    result = await runner.run(QUESTION, KIM, "s", history)
    sent = llm.calls[1][0]
    assert [message.role for message in sent] == ["system", "user", "user"]
    assert sent[1].content == QUESTION and NOTES in sent[2].content
    assert "동일 조건의 재등록은 기존 요청을 반환" in sent[2].content
    assert all("이전 요청은 메모가 달라" not in message.content for message in sent)
    assert any("이전 요청은 메모가 달라" in message.content for message in result.messages)
    assert approver.requests == ["submit"] and "중복 생성하지 않았습니다" in result.final_message.content
    assert store.get_case("REVIEW-001", KIM)["review_request"]["notes"] == NOTES
