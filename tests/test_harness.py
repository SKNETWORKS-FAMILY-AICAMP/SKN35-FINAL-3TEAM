"""harness 구현 테스트 (툴 타임아웃 등 계약 밖 요구사항). 계약 테스트는 test_harness_contract.py."""

from __future__ import annotations

import asyncio

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import Principal, ToolCall
from harness import create_harness
from tests.fakes import FakeRegistry, FixedApprover, MemoryAudit, ScriptedChecker, ScriptedLLM


class SlowTool:
    def __init__(self, name: str, delay: float):
        self.spec = ToolSpec(name=name, description="slow")
        self.delay = delay
        self.cancelled = False

    async def run(self, call: ToolCall) -> ToolResult:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult(call.call_id, True, "done")


class OwnTimeoutTool:
    spec = ToolSpec(name="http", description="raises its own TimeoutError")

    async def run(self, call: ToolCall) -> ToolResult:
        raise TimeoutError("upstream timeout")


def build(tools, tool_timeout):
    llm = ScriptedLLM([
        {"tool_calls": [{"id": f"c{i}", "name": t.spec.name, "arguments": {}} for i, t in enumerate(tools)]},
        {"content": "끝"},
    ])
    audit = MemoryAudit()
    harness = create_harness(
        llm=llm, tools=FakeRegistry(tools), checker=ScriptedChecker({}),
        approver=FixedApprover(True), audit=audit, max_turns=3, tool_timeout=tool_timeout,
    )
    return harness, audit


async def test_slow_tool_times_out_and_run_continues():
    slow, fast = SlowTool("slow", 5), SlowTool("fast", 0)
    harness, audit = build([slow, fast], tool_timeout=0.05)
    result = await harness.run("go", Principal("u"), "s1")
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert "timed out after 0.05s" in tool_msgs[0].content
    assert tool_msgs[1].content == "done"
    assert slow.cancelled
    assert result.stop_reason == "completed"
    assert [r.executed for r in audit.records] == [True, True]


async def test_tools_own_timeout_error_is_not_reported_as_harness_timeout():
    harness, _ = build([OwnTimeoutTool()], tool_timeout=5)
    result = await harness.run("go", Principal("u"), "s1")
    msg = next(m for m in result.messages if m.role == "tool")
    assert "raised TimeoutError: upstream timeout" in msg.content


async def test_tool_own_timeout_attribute_overrides_harness_default():
    # 오래 걸리는 툴(문서 파싱 등)은 timeout 속성으로 하네스 기본 제한시간을 바꾼다
    patient = SlowTool("patient", 0.1)
    patient.timeout = 5
    impatient = SlowTool("impatient", 5)
    impatient.timeout = 0.02
    harness, _ = build([patient, impatient], tool_timeout=0.05)
    result = await harness.run("go", Principal("u"), "s1")
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert tool_msgs[0].content == "done"
    assert "timed out after 0.02s" in tool_msgs[1].content


async def test_no_timeout_when_none():
    harness, _ = build([SlowTool("t", 0.01)], tool_timeout=None)
    result = await harness.run("go", Principal("u"), "s1")
    assert next(m for m in result.messages if m.role == "tool").content == "done"


# ── 거부 시 고정 문구 ───────────────────────────────────────

def test_template_reply_deny_includes_reason_and_done_results():
    from contracts.harness import Denial
    from harness.denial import DoneTool, template_reply

    text = template_reply(Denial("c2", "delete_file", "deny", "부서장만 삭제 가능"),
                          [DoneTool("summarize", "예산 10% 증액")])
    assert "부서장만 삭제 가능." in text and "summarize: 예산 10% 증액" in text
    assert text.index("완료된 작업") < text.index("delete_file")


def test_template_reply_rejected_does_not_blame_permission():
    from contracts.harness import Denial
    from harness.denial import template_reply

    text = template_reply(Denial("c1", "send_email", "rejected", "메일이 발송됩니다"))
    assert "승인하지 않으셔서" in text and "권한이 없어" not in text and "시간" not in text


def test_template_reply_timeout_differs_from_rejected():
    from contracts.harness import Denial
    from harness.denial import template_reply, wrapup_instruction

    text = template_reply(Denial("c1", "send_email", "timeout", "메일이 발송됩니다"))
    assert "응답이 없어" in text and "승인하지 않으셔서" not in text and "권한이 없어" not in text
    assert "응답하지 않아" in wrapup_instruction(Denial("c1", "send_email", "timeout", "x"), [])


def test_template_reply_uses_display_name():
    from contracts.harness import Denial
    from harness.denial import template_reply

    text = template_reply(Denial("c1", "multiply", "deny", "곱셈 금지", display_name="곱셈"))
    assert "'곱셈' 작업은" in text and "multiply" not in text


async def test_done_results_use_display_name_in_template():
    from tests.fakes import FakeTool

    llm = ScriptedLLM([
        {"tool_calls": [{"id": "c1", "name": "add", "arguments": {}}, {"id": "c2", "name": "mul", "arguments": {}}]},
        {"raises": "down"},
    ])
    harness = create_harness(
        llm=llm,
        tools=FakeRegistry([FakeTool("add", "7", display_name="덧셈"), FakeTool("mul", display_name="곱셈")]),
        checker=ScriptedChecker({"mul": "deny"}), approver=FixedApprover(True), audit=MemoryAudit(), max_turns=3,
    )
    result = await harness.run("go", Principal("u"), "s1")
    assert "- 덧셈: 7" in result.final_message.content and "'곱셈' 작업은" in result.final_message.content


def test_wrapup_instruction_mentions_kind_reason_and_results():
    from contracts.harness import Denial
    from harness.denial import DoneTool, wrapup_instruction

    text = wrapup_instruction(Denial("c2", "delete_file", "deny", "부서장만 삭제 가능"), [DoneTool("summarize", "x" * 1000)])
    assert "부서장만 삭제 가능" in text and "우회" in text
    assert "x" * 300 + "…" in text and "x" * 301 not in text


def test_template_reply_adds_period_after_polite_ending():
    from contracts.harness import Denial
    from harness.denial import template_reply

    text = template_reply(Denial("c1", "multiply", "deny", "곱셈은 재무팀만 쓸 수 있습니다"))
    assert "있습니다. 필요하면" in text


# ── LLM 없이 데모 (LocalMathClient) ─────────────────────────

async def _math(text, tools=("add", "subtract", "multiply"), checker=None):
    from harness.local_math import LocalMathClient
    from tools.arithmetic import arithmetic_tools
    from tools.registry import SimpleToolRegistry

    registry = SimpleToolRegistry(arithmetic_tools()).only(tools)
    harness = create_harness(
        llm=LocalMathClient(), tools=registry, checker=checker or ScriptedChecker({}),
        approver=FixedApprover(True), audit=MemoryAudit(), max_turns=5,
    )
    return await harness.run(text, Principal("u"), "s1")


async def test_local_math_runs_tools():
    assert (await _math("7 * 8")).final_message.content == "계산 결과는 56입니다."
    assert (await _math("7과 8을 곱해줘")).final_message.content == "계산 결과는 56입니다."
    r = await _math("(2 + 3) * 4")
    assert r.final_message.content == "계산 결과는 20입니다." and r.llm_calls == 3


async def test_local_math_respects_agent_tools_and_denial():
    r = await _math("7 * 8", tools=("add",))
    assert "multiply" in r.final_message.content and r.llm_calls == 1
    r = await _math("2 + 3 은?", checker=ScriptedChecker({"add": "deny"}))
    assert r.stop_reason == "denied" and "권한이 없어" in r.final_message.content


async def test_tool_ok_is_false_on_timeout():
    harness, audit = build([SlowTool("slow", 5)], tool_timeout=0.01)
    await harness.run("go", Principal("u"), "s1")
    assert [(r.executed, r.tool_ok) for r in audit.records] == [(True, False)]
