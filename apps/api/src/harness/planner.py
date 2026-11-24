"""플래너: 직원 요청을 "어떤 툴을 어떤 순서로 쓸지" 계획으로 바꾼다.

흐름 (harness/plan_runner.py 가 실행한다):
  1. make_plan   LLM 이 에이전트의 툴 목록·역할 설명을 보고 JSON 계획을 낸다. {"steps": [{"tool", "goal"}]}
  2. check_plan  코드가 계획을 검사·보정한다. LLM 의 계획을 그대로 믿지 않는다 (작은 모델은 조회를 자주 빠뜨린다).
                 - 에이전트에 없는 툴, 겹치는 단계는 뺀다
                 - 저장·발송(write·send) 단계는 직원이 만들기·저장·보내기를 분명히 요청했을 때만 남긴다
                 - 조회 규칙(harness/grounding.py)이 필요하다고 보는 조회가 빠졌으면 앞에 넣는다
                 - 앞 단계 결과가 필요한 툴(REQUIRES)은 그 단계를 앞에 넣는다 (예: 리포트 저장 앞에 시장 조회)
  3. 계획을 못 읽으면(JSON 아님·모델 오류) 빈 계획에서 2번 보정만 한 계획을 쓴다 (fallback).
툴 실행은 계획의 단계마다 그 툴 하나만 LLM 에게 붙여서 한다. 권한 검사·감사 로그는 기존 경로 그대로다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from contracts.harness import LLMClient, Message, ToolSpec
from harness.grounding import LookupRule
from harness.market_grounding import wants_report

log = logging.getLogger(__name__)

EFFECT_LABEL = {"read": "조회", "write": "저장", "send": "발송"}
# 저장·발송 툴은 이런 요청이 있을 때만 계획에 남긴다
_ASKS_ACTION = re.compile(r"만들|작성|생성|저장|보내|발송|등록|올려|넣어|정리표|정리해|뽑아|추가|알림함|받을|받고|PDF|pdf|DOCX|docx")

# 툴 → 그 툴보다 먼저 성공해야 하는 툴들 (에이전트에 있는 것만 넣는다)
REQUIRES: dict[str, tuple[str, ...]] = {
    "get_report_template": ("search_report_templates",),
    "save_morning_report": ("search_report_templates", "get_report_template", "market_snapshot"),
    "get_doc_chunk": ("search_doc",),
    "send_maturity_alert": ("loans_maturing",),
}
# 발송 툴 → 직원이 먼저 확인해야 하는 준비 툴. 준비가 같은 계획에 있으면 발송은 빼고, 다음 요청("보내 줘")에서 한다
CONFIRM_FIRST: dict[str, str] = {
    "send_maturity_notice": "prepare_maturity_notices",
}

PLANNER_PROMPT = """너는 은행 업무 에이전트의 계획 담당이다. 직원 요청을 처리할 단계를 정한다.
각 단계는 아래 도구 중 하나를 쓰는 일이다. 도구를 직접 호출하지 말고 계획만 쓴다.

사용할 수 있는 도구:
{tools}

규칙:
- 도구 이름은 위 목록에 있는 것만 그대로 쓴다.
- 규정·수치·고객·대출 정보가 필요한 답이면 그 정보를 조회하는 단계를 반드시 넣는다. 조회 없이 답하는 계획을 쓰지 않는다.
- 저장·발송 도구는 직원이 만들기·저장·보내기를 분명히 요청했을 때만 넣는다.
- 앞 단계 결과가 필요한 단계는 그 뒤에 둔다.
- 도구가 필요 없는 요청(인사, 일반 개념 설명)이면 steps 를 빈 목록으로 둔다.
- 단계는 최대 {limit}개다. 최종 답 쓰기는 단계에 넣지 않는다.

에이전트 역할 (단계 순서의 참고):
{role}

출력 형식 (JSON 객체 하나만, 설명 없이):
{{"steps": [{{"tool": "도구 이름", "goal": "이 단계에서 할 일 한 문장"}}]}}"""


@dataclass(frozen=True)
class PlanStep:
    tool: str
    goal: str = ""
    added_by: str = "planner"  # planner | rule | requires | fallback (어디서 넣은 단계인가)


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    source: str = "llm"  # llm | fallback
    notes: list[str] = field(default_factory=list)  # 검사가 고친 내용 (로그용)

    @property
    def tools(self) -> list[str]:
        return [s.tool for s in self.steps]

    def describe(self) -> str:
        return " → ".join(f"{s.tool}({s.added_by})" for s in self.steps) or "(도구 없음)"


def parse_plan(text: str) -> list[PlanStep] | None:
    """모델 답에서 계획을 꺼낸다. 코드 블록·앞뒤 설명이 섞여 있어도 첫 { 부터 마지막 } 까지 본다."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return None
    out = []
    for item in steps:
        if isinstance(item, str):
            out.append(PlanStep(item.strip()))
        elif isinstance(item, dict) and isinstance(item.get("tool"), str):
            out.append(PlanStep(item["tool"].strip(), str(item.get("goal") or "")[:300]))
    return out


def _tool_lines(specs: list[ToolSpec]) -> str:
    return "\n".join(f"- {s.name} ({EFFECT_LABEL.get(s.effect, s.effect)}): {s.description}" for s in specs)


def _recent(history: list[Message], limit: int = 4) -> str:
    """앞선 대화 중 사용자·답변 메시지 몇 개 (예: "보낼까요?" 다음의 "응 보내줘" 를 이해하게)."""
    lines = [f"{m.role}: {m.content[:300]}" for m in history if m.role in ("user", "assistant") and m.content]
    return "\n".join(lines[-limit:])


async def make_plan(
    llm: LLMClient, question: str, history: list[Message], specs: list[ToolSpec],
    rules: tuple[LookupRule, ...], *, limit: int,
) -> Plan:
    role = next((m.content for m in history if m.role == "system"), "")
    prompt = PLANNER_PROMPT.format(tools=_tool_lines(specs), limit=limit, role=role[:1500] or "(없음)")
    recent = _recent(history)
    # /no_think: qwen3 가 생각 없이 바로 JSON 을 쓰게 한다 (사용자 메시지 끝에 있어야 듣는다)
    user = (f"앞선 대화:\n{recent}\n\n" if recent else "") + f"직원 요청:\n{question[:2000]}\n/no_think"
    steps: list[PlanStep] | None = None
    try:
        reply = (await llm.chat([Message("system", prompt), Message("user", user)], [])).message
        steps = parse_plan(reply.content)
    except Exception:
        log.warning("플래너 LLM 호출 실패, 규칙으로 계획을 만듭니다", exc_info=True)
    plan = Plan(steps or [], "llm" if steps is not None else "fallback")
    return check_plan(plan, question, specs, rules, limit=limit)


def check_plan(plan: Plan, question: str, specs: list[ToolSpec], rules: tuple[LookupRule, ...], *, limit: int) -> Plan:
    """LLM 계획을 검사·보정한다 (모듈 설명 2번). plan 을 고쳐서 돌려준다."""
    by_name = {s.name: s for s in specs}
    asks_action = bool(_ASKS_ACTION.search(question))
    kept: list[PlanStep] = []
    for step in plan.steps:
        spec = by_name.get(step.tool)
        if spec is None:
            plan.notes.append(f"없는 도구 뺌: {step.tool}")
        elif any(k.tool == step.tool for k in kept):
            plan.notes.append(f"겹치는 단계 뺌: {step.tool}")
        elif spec.effect in ("write", "send") and not asks_action:
            plan.notes.append(f"요청하지 않은 저장·발송 뺌: {step.tool}")
        elif step.tool == "save_morning_report" and not wants_report(question):
            plan.notes.append("리포트 저장 요청이 아니라 저장 단계를 뺌")
        else:
            kept.append(step)

    # 조회 규칙: 필요한 조회가 계획에 없으면 맨 앞에 넣는다 (규칙 순서대로)
    lookups: list[PlanStep] = []
    for rule in rules:
        tool = rule.available(by_name)
        planned = {s.tool for s in [*lookups, *kept]}
        if tool and not (planned & rule.grounded_tools) and rule.needed(question, ""):
            lookups.append(PlanStep(tool, f"{by_name[tool].label}로 필요한 정보를 조회한다", "rule"))
            plan.notes.append(f"조회 규칙으로 넣음: {tool}")
    # 리포트를 만들어 달라는데 저장 단계가 없으면 넣는다 (시장 리포트)
    if ("save_morning_report" in by_name and "market_snapshot" in {s.tool for s in [*lookups, *kept]}
            and wants_report(question) and not any(s.tool == "save_morning_report" for s in kept)):
        kept.append(PlanStep("save_morning_report", "조회 결과로 리포트를 저장한다", "rule"))
        plan.notes.append("리포트 요청이라 저장 단계를 넣음")

    # 앞 단계가 필요한 툴은 그 단계를 먼저 둔다
    ordered: list[PlanStep] = []

    def add(step: PlanStep) -> None:
        if any(s.tool == step.tool for s in ordered):
            return
        for need in REQUIRES.get(step.tool, ()):
            if need in by_name:
                found = next((s for s in [*lookups, *kept] if s.tool == need), None)
                add(found or PlanStep(need, f"{by_name[need].label} 결과가 다음 단계에 필요하다", "requires"))
        ordered.append(step)

    for step in [*lookups, *kept]:
        add(step)
    planned = {s.tool for s in ordered}
    for send, prepare in CONFIRM_FIRST.items():
        if send in planned and prepare in planned:
            ordered = [s for s in ordered if s.tool != send]
            plan.notes.append(f"{prepare} 결과를 직원이 확인한 뒤에 보내도록 {send} 를 뺌")
    if len(ordered) > limit:
        plan.notes.append(f"단계가 {len(ordered)}개라 {limit}개까지만 씀")
    plan.steps = ordered[:limit]
    return plan
