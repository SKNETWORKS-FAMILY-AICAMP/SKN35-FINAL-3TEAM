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

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field

from contracts.harness import LLMClient, Message, ToolSpec
from harness.attachment_grounding import refers_to_attachment
from harness.grounding import LookupRule, RequireSearch
from harness.market_grounding import wants_report

log = logging.getLogger(__name__)

# 계획 한 번에 기다리는 시간(초). 넘으면 규칙만으로 계획한다
# (2026-11-24 실측: qwen3 8b 가 /no_think 를 무시하고 생각만 하다 4분 걸린 적이 있음)
PLANNER_TIMEOUT = float(os.environ.get("HARNESS_PLANNER_TIMEOUT_SECONDS", "60"))
# 첨부를 읽어도 사내 문서 검색이 따로 필요한 질문 (첨부를 사내 규정과 비교 등)
_CORPORATE = re.compile(r"사내|내규|우리\s*(?:은행|회사|규정)|규정|지침")

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
# 메일 발송(send_mail)은 보내 달라는 요청이고 초안 요청이 아닐 때만 계획에 둔다.
# 받는 사람은 단계에서 코드가 직원 목록으로 정하고(harness/steps.py), 못 찾으면 보내지 않고 초안만 쓴다.
# 초안만 달라고 하면 답에 초안('제목: ...' + 본문)을 쓰고, 화면의 '메일로 보내기'에서 직원을 골라 보낸다
_SEND_VERB = re.compile(r"보내|발송|전송")
_DRAFT = re.compile(r"초안|draft", re.I)

PLANNER_PROMPT = """너는 은행 업무 에이전트의 계획 담당이다. 직원 요청을 처리할 단계를 정한다.
각 단계는 아래 도구 중 하나를 쓰는 일이다. 도구를 직접 호출하지 말고 계획만 쓴다.

사용할 수 있는 도구:
{tools}

규칙:
- 도구 이름은 위 목록에 있는 것만 그대로 쓴다.
- 규정·수치·고객·대출 정보가 필요한 답이면 그 정보를 조회하는 단계를 반드시 넣는다. 조회 없이 답하는 계획을 쓰지 않는다.
- 저장·발송 도구는 직원이 만들기·저장·보내기를 분명히 요청했을 때만 넣는다.
- 앞 단계 결과가 필요한 단계는 그 뒤에 둔다.
- 메일을 보내 달라는 요청이면 메일 보내기 도구를 넣는다. 받는 사람은 서버가 직원 목록에서 정한다.
  초안만 써 달라는 요청이면 넣지 않는다 (초안은 답으로 쓰고, 직원이 화면에서 받는 사람을 골라 보낸다).
- 첨부 파일 내용이 필요한 요청이면 parse_doc 을 첫 단계로 넣는다. 어떤 파일을 읽을지는 서버가 정한다.
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
    rules: tuple[LookupRule, ...], *, limit: int, files: list[dict] | None = None,
) -> Plan:
    role = next((m.content for m in history if m.role == "system"), "")
    prompt = PLANNER_PROMPT.format(tools=_tool_lines(specs), limit=limit, role=role[:1500] or "(없음)")
    recent = _recent(history)
    # /no_think: qwen3 가 생각 없이 바로 JSON 을 쓰게 한다 (사용자 메시지 끝에 있어야 듣는다)
    attached = ", ".join(f["filename"] + (" (이번에 첨부)" if f.get("new") else "") for f in files or [])
    user = ((f"앞선 대화:\n{recent}\n\n" if recent else "") + (f"첨부 파일: {attached}\n\n" if attached else "")
            + f"직원 요청:\n{question[:2000]}\n/no_think")
    steps: list[PlanStep] | None = None
    try:
        reply = (await asyncio.wait_for(llm.chat([Message("system", prompt), Message("user", user)], []),
                                        PLANNER_TIMEOUT)).message
        steps = parse_plan(reply.content)
    except TimeoutError:
        log.warning("플래너가 %g초 안에 답하지 않아 규칙으로 계획을 만듭니다", PLANNER_TIMEOUT)
    except Exception:
        log.warning("플래너 LLM 호출 실패, 규칙으로 계획을 만듭니다", exc_info=True)
    plan = Plan(steps or [], "llm" if steps is not None else "fallback")
    return check_plan(plan, question, specs, rules, limit=limit, files=files)


def check_plan(plan: Plan, question: str, specs: list[ToolSpec], rules: tuple[LookupRule, ...], *, limit: int,
               files: list[dict] | None = None) -> Plan:
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
        elif step.tool == "send_mail" and (_DRAFT.search(question) or not _SEND_VERB.search(question)):
            plan.notes.append("초안 요청이거나 보내 달라는 요청이 아니라 send_mail 을 뺌 (화면에서 직원을 골라 보냄)")
        elif step.tool == "parse_doc" and not files:
            plan.notes.append("첨부가 없어 parse_doc 을 뺌")
        elif step.tool == "save_morning_report" and not wants_report(question):
            plan.notes.append("리포트 저장 요청이 아니라 저장 단계를 뺌")
        else:
            kept.append(step)

    # 첨부: 질문이 첨부를 가리키면 parse_doc 을 넣고, 읽기는 항상 첫 단계로 둔다 (다음 단계가 원문을 쓴다)
    if files and "parse_doc" in by_name:
        reading = next((s for s in kept if s.tool == "parse_doc"), None)
        if reading is None and refers_to_attachment(question, files):
            reading = PlanStep("parse_doc", "첨부 문서에서 질문과 관련된 원문을 읽는다", "rule")
            plan.notes.append("첨부를 가리키는 질문이라 parse_doc 을 넣음")
        if reading is not None:
            kept = [reading, *(s for s in kept if s.tool != "parse_doc")]

    # 조회 규칙: 필요한 조회가 계획에 없으면 맨 앞에 넣는다 (규칙 순서대로)
    # 첨부 읽기는 첨부를 가리키는 질문에서만 사내 문서 조회를 대신한다. 사내 규정과 비교하는 질문이면 검색도 한다
    attachment_only = bool(files) and refers_to_attachment(question, files) and not _CORPORATE.search(question)
    lookups: list[PlanStep] = []
    for rule in rules:
        tool = rule.available(by_name)
        planned = {s.tool for s in [*lookups, *kept]}
        grounded = rule.grounded_tools
        if isinstance(rule, RequireSearch) and not attachment_only:
            grounded = grounded - {"parse_doc"}
        if tool and not (planned & grounded) and rule.needed(question, ""):
            lookups.append(PlanStep(tool, f"{by_name[tool].label}로 필요한 정보를 조회한다", "rule"))
            plan.notes.append(f"조회 규칙으로 넣음: {tool}")
    # 리포트를 만들어 달라는데 저장 단계가 없으면 넣는다 (시장 리포트)
    if ("save_morning_report" in by_name and "market_snapshot" in {s.tool for s in [*lookups, *kept]}
            and wants_report(question) and not any(s.tool == "save_morning_report" for s in kept)):
        kept.append(PlanStep("save_morning_report", "조회 결과로 리포트를 저장한다", "rule"))
        plan.notes.append("리포트 요청이라 저장 단계를 넣음")

    # 메일을 보내 달라는데 계획에 메일 보내기가 없으면 넣는다 (고객 만기 안내 메일은 그 전용 도구가 맡는다)
    planned_now = {s.tool for s in [*lookups, *kept]}
    if ("send_mail" in by_name and "send_mail" not in planned_now and "메일" in question
            and _SEND_VERB.search(question) and not _DRAFT.search(question)
            and not planned_now & {"prepare_maturity_notices", "send_maturity_notice"}):
        kept.append(PlanStep("send_mail", "요청한 내용으로 메일을 써서 보낸다", "rule"))
        plan.notes.append("메일을 보내 달라는 요청이라 send_mail 을 넣음")

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
    ordered.sort(key=lambda s: s.tool != "parse_doc")  # 첨부 읽기가 맨 앞 (나머지 순서는 그대로)
    planned = {s.tool for s in ordered}
    for send, prepare in CONFIRM_FIRST.items():
        if send in planned and prepare in planned:
            ordered = [s for s in ordered if s.tool != send]
            plan.notes.append(f"{prepare} 결과를 직원이 확인한 뒤에 보내도록 {send} 를 뺌")
    if len(ordered) > limit:
        plan.notes.append(f"단계가 {len(ordered)}개라 {limit}개까지만 씀")
    plan.steps = ordered[:limit]
    return plan


def require_regulation_steps(plan: Plan, specs: dict[str, ToolSpec]) -> None:
    """규정 모드: 사내 문서 검색이 계획에 있어야 하고, 원문 열람 툴이 있으면 검색 바로 뒤에 둔다."""
    if "search_doc" not in plan.tools:
        plan.steps.insert(0, PlanStep("search_doc", "사내 문서에서 관련 규정을 찾는다", "rule"))
        plan.notes.append("규정 질문이라 search_doc 을 넣음")
    if "get_doc_chunk" in specs:
        plan.steps = [s for s in plan.steps if s.tool != "get_doc_chunk"]
        at = next(i for i, s in enumerate(plan.steps) if s.tool == "search_doc") + 1
        plan.steps.insert(at, PlanStep("get_doc_chunk", "찾은 규정의 원문을 읽는다", "rule"))
