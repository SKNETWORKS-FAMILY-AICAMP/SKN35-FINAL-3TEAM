"""하네스 공통 테스트: 모든 구현 × 모든 시나리오.

실행:  uv run pytest tests/test_harness_contract.py -v
특정 구현만:  HARNESS_IMPLS=harness.son:create_harness uv run pytest ...
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from contracts.harness import AgentLoop, Message, RunResult
from contracts.permission import Principal
from tests.fakes import ScenarioWorld
from tests.implementations import HARNESS_IMPLS, impl_ids, load_factory

FIXTURE = Path(__file__).parent.parent / "data/fixtures/harness_scenarios.json"
SCENARIOS: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]


@pytest.fixture(params=impl_ids("HARNESS_IMPLS", HARNESS_IMPLS))
def factory(request):
    return load_factory(request.param)


async def run_scenario(factory, sc: dict[str, Any]) -> tuple[RunResult, ScenarioWorld]:
    world = ScenarioWorld.from_scenario(sc)
    harness = factory(
        llm=world.llm,
        tools=world.tools,
        checker=world.checker,
        approver=world.approver,
        audit=world.audit,
        max_turns=sc["max_turns"],
    )
    assert isinstance(harness, AgentLoop), "create_harness 가 AgentLoop 를 돌려주지 않음"
    result = await harness.run(sc["user_input"], Principal("tester", ("user",)), "session-1")
    return result, world


def tool_messages(messages: list[Message]) -> dict[str, Message]:
    return {m.tool_call_id: m for m in messages if m.role == "tool" and m.tool_call_id}


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
async def test_scenario(factory, sc):
    result, world = await run_scenario(factory, sc)
    exp = sc["expected"]

    # 종료 상태
    assert result.stop_reason == exp["stop_reason"]
    assert result.llm_calls == exp["llm_calls"] == len(world.llm.calls)
    if exp.get("final_content", "") is None:
        assert result.final_message is None
    else:
        assert result.final_message is not None
        assert result.final_message.role == "assistant"
        assert result.final_message.content, "final_message.content 가 비어 있음"
        assert result.messages[-1] == result.final_message
        if "final_content" in exp:
            assert result.final_message.content == exp["final_content"]
        if "final_contains" in exp:
            assert exp["final_contains"] in result.final_message.content

    # 거부로 멈춤 (규칙 8)
    if "denial" in exp:
        assert result.denial is not None
        assert (result.denial.kind, result.denial.tool_name) == (exp["denial"]["kind"], exp["denial"]["tool_name"])
        assert result.denial.reason, "Denial.reason 이 비어 있음"
    elif result.stop_reason != "denied":
        assert result.denial is None
    if "wrapup_call" in exp:
        last_tools = world.llm.calls[-1][1]
        has_wrapup = len(world.llm.calls) > 1 and last_tools == []
        assert has_wrapup is exp["wrapup_call"]
    # 마무리 지시문 등 하네스가 만든 user 메시지는 기록에 남지 않는다
    assert [m.content for m in result.messages if m.role == "user"] == [sc["user_input"]]

    # 툴 실행
    if "executed" in exp:
        assert world.executed == exp["executed"]

    # LLM 에게 돌려준 툴 메시지
    if "tool_messages" in exp:
        tms = tool_messages(result.messages)
        for tm in exp["tool_messages"]:
            assert tm["call_id"] in tms, f"{tm['call_id']} 에 대한 role=tool 메시지가 없음"
            content = tms[tm["call_id"]].content
            assert content, "툴 메시지 content 가 비어 있음"
            assert tm["contains"] in content

    # 승인 요청
    if "approval_requests" in exp:
        assert world.approver.requests == exp["approval_requests"]

    # 감사 로그
    if "audit" in exp:
        got = [
            {
                "call_id": r.call.call_id,
                "decision": r.result.decision.value,
                "approval": r.approval,
                "executed": r.executed,
            }
            for r in world.audit.records
        ]
        assert got == exp["audit"]


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
async def test_llm_input_invariants(factory, sc):
    """모든 시나리오에 공통으로 지켜야 하는 규칙."""
    _, world = await run_scenario(factory, sc)
    tool_names = sorted(t["name"] for t in sc.get("tools", []))

    wrapup = sc["expected"].get("wrapup_call", False)
    for i, (messages, tools) in enumerate(world.llm.calls):
        # 등록된 툴 목록을 LLM 에게 넘긴다 (거부 후 마무리 호출만 tools=[])
        if wrapup and i == len(world.llm.calls) - 1:
            assert tools == []
        else:
            assert sorted(t.name for t in tools) == tool_names
        # 사용자 입력이 들어 있다
        assert any(m.role == "user" and m.content == sc["user_input"] for m in messages)
        # 이전 응답의 tool_call 마다 짝이 맞는 tool 메시지가 있다
        if i > 0:
            requested = {tc.call_id for m in messages if m.role == "assistant" for tc in m.tool_calls}
            answered = set(tool_messages(messages))
            assert requested <= answered, f"응답 안 된 tool_call: {requested - answered}"


async def test_principal_and_session_passed_to_checker(factory):
    sc = next(s for s in SCENARIOS if s["id"] == "single_tool")
    _, world = await run_scenario(factory, sc)
    call = world.checker.calls[0]
    assert call.principal == Principal("tester", ("user",))
    assert call.session_id == "session-1"
    assert call.call_id == "c1"
    assert call.arguments == {"q": "SK 뉴스"}


async def test_history_is_kept(factory):
    sc = next(s for s in SCENARIOS if s["id"] == "direct_answer")
    world = ScenarioWorld.from_scenario(sc)
    harness = factory(
        llm=world.llm, tools=world.tools, checker=world.checker,
        approver=world.approver, audit=world.audit, max_turns=5,
    )
    history = [Message(role="user", content="이전 질문"), Message(role="assistant", content="이전 답")]
    result = await harness.run("안녕?", Principal("tester"), "session-1", history=history)

    sent = world.llm.calls[0][0]
    assert any(m.content == "이전 질문" for m in sent)
    assert result.messages[: len(history)] == history


async def test_agent_and_run_id_reach_every_tool_call(factory):
    sc = next(s for s in SCENARIOS if s["id"] == "parallel_tools")
    world = ScenarioWorld.from_scenario(sc)
    harness = factory(
        llm=world.llm, tools=world.tools, checker=world.checker,
        approver=world.approver, audit=world.audit, max_turns=5,
    )
    await harness.run(sc["user_input"], Principal("tester"), "session-1", agent_id="agent-7", run_id="run-1")
    assert [(c.agent_id, c.run_id) for c in world.checker.calls] == [("agent-7", "run-1")] * 2
    assert all(r.call.agent_id == "agent-7" for r in world.audit.records)


async def test_agent_id_defaults_to_none(factory):
    sc = next(s for s in SCENARIOS if s["id"] == "single_tool")
    _, world = await run_scenario(factory, sc)
    assert world.checker.calls[0].agent_id is None


def test_tool_spec_label_falls_back_to_name():
    from contracts.harness import Denial, ToolSpec

    assert ToolSpec("send_email", "d").label == "send_email"
    assert ToolSpec("send_email", "d", display_name="메일 발송").label == "메일 발송"
    assert Denial("c1", "send_email", "deny", "r").label == "send_email"
    assert Denial("c1", "send_email", "deny", "r", "메일 발송").label == "메일 발송"


async def test_denial_carries_display_name(factory):
    sc = next(s for s in SCENARIOS if s["id"] == "ask_timeout")
    sc = {**sc, "tools": [{**sc["tools"][0], "display_name": "메일 발송"}]}
    result, _ = await run_scenario(factory, sc)
    assert result.denial is not None and result.denial.display_name == "메일 발송"


def test_message_round_trips_through_dict():
    from contracts.harness import LLMToolCall

    msgs = [
        Message("system", "sys"),
        Message("assistant", "", (LLMToolCall("c1", "search", {"q": "휴가", "n": [1, 2]}),)),
        Message("tool", "결과", tool_call_id="c1"),
    ]
    assert [Message.from_dict(m.to_dict()) for m in msgs] == msgs
