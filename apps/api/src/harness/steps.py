"""계획의 단계마다 붙는 툴별 처리 (harness/plan_runner.py 가 부른다).

툴마다 두 가지를 정할 수 있다.
  bind(ctx)          단계를 실행하기 전. 인자를 코드가 정할 수 있으면 정한다 (LLM 호출 없이).
                     - Args(dict)   이 인자로 바로 실행
                     - Finish(text) 실행하지 않고 이 문구로 끝냄 (예: 기준일을 모름 → 되묻기)
                     - None         LLM 에게 이 툴 하나만 주고 인자를 정하게 한다
  after(ctx, ok, content)  단계를 실행한 뒤.
                     - Finish(text) 이 문구로 끝냄 (예: 저장한 리포트 본문을 그대로 보여 줌)
                     - Args(dict)   같은 툴을 이 인자로 한 번 더 실행 (예: 문서 검색 0건 → 검색어를 바꿔 재검색)
                     - None         다음 단계로
규칙이 없는 툴은 LLM 이 인자를 정하고, 결과는 마지막 답 쓰기에서 LLM 이 읽는다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from contracts.harness import LLMClient, Message, ToolSpec
from harness.attachment_grounding import extractive_reply, parse_record, pick_files, read_call
from harness.grounding import RequireSearch
from harness.market_grounding import CLARIFY_DATE, REPORT_ID, base_date_args, template_query
from harness.regulation import UNAVAILABLE, Evidence, candidates, read_evidence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Args:
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Calls:
    """같은 툴을 여러 번 (예: 첨부 파일마다 한 번씩)."""

    arguments: list[dict[str, Any]]


@dataclass(frozen=True)
class Finish:
    text: str


@dataclass
class StepContext:
    question: str
    llm: LLMClient
    specs: dict[str, ToolSpec]
    later: list[str]  # 이번 단계 뒤에 남은 툴 이름
    results: dict[str, Any] = field(default_factory=dict)  # 툴 이름 → 마지막 성공 결과 (JSON 이면 dict)
    tried_queries: set[str] = field(default_factory=set)
    retried: set[str] = field(default_factory=set)  # 한 번 더 실행한 툴 (run 당 한 번)
    llm_calls: int = 0  # 단계 처리 중 쓴 LLM 호출 (요약 등)
    llm_budget: int = 0  # 단계 처리에 더 쓸 수 있는 LLM 호출 수
    files: list[dict] = field(default_factory=list)  # 대화에 연결된 첨부 (서버 확인, file_id·filename·new)
    reading: list[dict] = field(default_factory=list)  # 이번에 읽는 첨부
    records: list[dict] = field(default_factory=list)  # 읽은 첨부 원문 (parse_record)
    read_args: dict[str, dict] = field(default_factory=dict)  # file_id → 마지막으로 읽은 인자 (이어 읽기용)
    # 규정 모드 (harness/regulation.py): 검색 후보를 점수·주제로 거르고, 원문을 읽어 인용만 답한다
    regulation: bool = False
    min_score: float = 0.30
    candidates: list[dict] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


def load(content: str) -> Any:
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return content


def _error(content: str) -> str:
    return content.removeprefix("error: ")


# ── 사내 문서 (규정 안내) ─────────────────────────────────────

_DOC_RULE = RequireSearch()


def _hit_count(data: Any) -> int:
    """문서 검색 결과의 건수. 모양을 모르면 1 (0건으로 잘못 보지 않게)."""
    matches = data.get("matches") if isinstance(data, dict) else None
    return len(matches) if isinstance(matches, list) else 1


async def bind_search_doc(ctx: StepContext):
    if ctx.regulation:  # 규정 모드: 질문 원문으로 먼저 찾는다 (없으면 핵심 단어로 한 번 더)
        return Args({_DOC_RULE.query_arg: ctx.question.strip()[:2000]})
    return None  # 검색어는 LLM 이 정한다. 못 정하면 plan_runner 가 규칙의 인자(질문 핵심 단어)를 쓴다


def _after_regulation_search(ctx: StepContext, ok: bool, content: str):
    if not ok:
        return Finish("문서 검색을 완료하지 못해 규정의 근거를 확인할 수 없습니다. 잠시 뒤 다시 시도해 주세요.")
    ctx.candidates = candidates(content, ctx.question, ctx.min_score)
    if not ctx.candidates:
        retry = _DOC_RULE.arguments(ctx.question)
        if "search_doc" not in ctx.retried and retry[_DOC_RULE.query_arg] not in ctx.tried_queries:
            ctx.retried.add("search_doc")
            return Args(retry)
        return Finish(UNAVAILABLE)
    if "get_doc_chunk" not in ctx.specs:  # 원문 열람 툴이 없으면 잘리지 않은 미리보기만 근거로
        ctx.evidence = [Evidence(m["chunk_id"], m["title"], str(m.get("location") or ""), m["preview"])
                        for m in ctx.candidates if m.get("is_truncated") is False]
    return None


def after_search_doc(ctx: StepContext, ok: bool, content: str, arguments: dict):
    ctx.tried_queries.add(str(arguments.get(_DOC_RULE.query_arg, "")))
    if ctx.regulation:
        return _after_regulation_search(ctx, ok, content)
    if not ok or _hit_count(load(content)):
        return None
    if "search_doc" not in ctx.retried:  # 0건이면 검색어를 바꿔 한 번 더 (결정 사항 1번)
        retry = _DOC_RULE.retry_arguments(ctx.question, ctx.tried_queries)
        if retry:
            ctx.retried.add("search_doc")
            return Args(retry)
    if "get_doc_chunk" in ctx.later:
        return None
    return Finish(_DOC_RULE.unavailable)


async def bind_get_doc_chunk(ctx: StepContext):
    if not ctx.regulation:
        return None  # 어떤 문단을 열지는 LLM 이 검색 결과를 보고 정한다
    if not ctx.candidates:
        return Finish(UNAVAILABLE)
    return Calls([{"chunk_id": m["chunk_id"]} for m in ctx.candidates])


def after_get_doc_chunk(ctx: StepContext, ok: bool, content: str, arguments: dict):
    if ctx.regulation and ok:
        match = next((m for m in ctx.candidates if m["chunk_id"] == arguments.get("chunk_id")), None)
        record = read_evidence(content, match) if match else None
        if record:
            ctx.evidence.append(record)
    return None


# ── 첨부 문서 ───────────────────────────────────────────────

READ_FAILED = "첨부 문서를 읽는 과정에서 오류가 발생했습니다. 아래 도구 실행 결과를 확인해 주세요. 문서에 내용이 없다는 뜻은 아닙니다."


async def bind_parse_doc(ctx: StepContext):
    """읽을 파일과 인자는 서버가 정한다. LLM 이 file_id 를 지어내거나 다른 사람 파일을 고를 수 없다."""
    ctx.reading = pick_files(ctx.question, ctx.files)
    if not ctx.reading:
        return Finish("이 대화에 연결된 첨부 파일이 없습니다. 읽을 파일을 첨부해 주세요.")
    return Calls([read_call(f, ctx.question).arguments for f in ctx.reading])


def after_parse_doc(ctx: StepContext, ok: bool, content: str, arguments: dict):
    file = next((f for f in ctx.reading if f["file_id"] == arguments.get("file_id")), None)
    record = parse_record(content, file["filename"]) if ok and file else None
    if record is None:
        return Finish(READ_FAILED)
    ctx.records.append(record)
    ctx.read_args[file["file_id"]] = dict(arguments)
    if len(ctx.records) == len(ctx.reading) and not any(r["text"].strip() for r in ctx.records):
        return Finish(extractive_reply(ctx.records))  # 질문과 맞는 문단이 없음
    return None


# ── 금리·환율 리포트 ──────────────────────────────────────────

async def bind_search_report_templates(ctx: StepContext):
    return Args({"query": template_query(ctx.question)})


def after_search_report_templates(ctx: StepContext, ok: bool, content: str, arguments: dict):
    if not ok:
        return Finish("보고서 양식을 검색하지 못했습니다. " + _error(content))
    data = load(content)
    matches = data.get("matches", []) if isinstance(data, dict) else []
    if "get_report_template" not in ctx.later:  # 양식 목록만 물었다
        return Finish(data.get("markdown", "")) if not ctx.later else None
    if not matches:
        return Finish(data.get("markdown", "") + "\n\n사용할 양식이 없어 보고서를 저장하지 않았습니다.")
    if len(matches) > 1 and matches[0]["score"] == matches[1]["score"]:
        return Finish(data.get("markdown", "") + "\n\n어느 양식을 사용할지 양식 이름을 알려 주세요.")
    return None


async def bind_get_report_template(ctx: StepContext):
    found = ctx.results.get("search_report_templates")
    matches = found.get("matches", []) if isinstance(found, dict) else []
    if not matches:
        return Finish("보고서 양식을 먼저 찾지 못해 리포트를 만들지 않았습니다.")
    return Args({"template_id": matches[0]["template_id"], "expected_hash": matches[0]["template_hash"]})


def after_get_report_template(ctx: StepContext, ok: bool, content: str, arguments: dict):
    return None if ok else Finish("선택한 보고서 양식을 읽지 못했습니다. " + _error(content))


async def bind_market_snapshot(ctx: StepContext):
    args = base_date_args(ctx.question)
    return Finish(CLARIFY_DATE) if args is None else Args(args)  # 날짜는 질문에서만 정한다 (추측 금지)


def after_market_snapshot(ctx: StepContext, ok: bool, content: str, arguments: dict):
    if not ok:
        return Finish("시장 도구 실행을 완료하지 못했습니다.\n\n" + _error(content))
    data = load(content)
    if not ctx.later and isinstance(data, dict):  # 조회만 요청: 서버가 만든 표를 그대로 보여 준다 (LLM 호출 없음)
        return Finish(data.get("text", data.get("markdown", "")))
    return None


async def bind_get_morning_report(ctx: StepContext):
    found = REPORT_ID.search(ctx.question)
    return Args({"report_id": found.group()}) if found else None


def after_get_morning_report(ctx: StepContext, ok: bool, content: str, arguments: dict):
    if not ok:
        return Finish("저장된 리포트를 열지 못했습니다. " + _error(content))
    data = load(content)
    return Finish(data.get("text", data.get("markdown", ""))) if isinstance(data, dict) else None


async def bind_save_morning_report(ctx: StepContext):
    data = ctx.results.get("market_snapshot")
    if not isinstance(data, dict):
        return Finish("금리·환율을 조회하지 못해 리포트를 저장하지 않았습니다.")
    selection = ctx.results.get("get_report_template")
    selection = selection if isinstance(selection, dict) else None
    summary = ""
    if not re.search(r"표만|요약\s*없이", ctx.question) and ctx.llm_budget > 0:
        summary, used = await market_summary(ctx.llm, data, selection, max_calls=ctx.llm_budget)
        ctx.llm_calls += used
    args = {"snapshot_id": data["snapshot_id"], "summary": summary}
    if selection:
        args["template_selection_id"] = selection["template_selection_id"]
    return Args(args)


def after_save_morning_report(ctx: StepContext, ok: bool, content: str, arguments: dict):
    if not ok:
        data = ctx.results.get("market_snapshot") or {}
        return Finish(data.get("markdown", "") + "\n\n리포트 저장 성공을 확인하지 못했습니다. " + _error(content))
    saved = load(content)
    return Finish(saved.get("text", saved.get("markdown", ""))) if isinstance(saved, dict) else None


BIND = {
    "get_doc_chunk": bind_get_doc_chunk,
    "parse_doc": bind_parse_doc,
    "search_doc": bind_search_doc,
    "search_report_templates": bind_search_report_templates,
    "get_report_template": bind_get_report_template,
    "market_snapshot": bind_market_snapshot,
    "get_morning_report": bind_get_morning_report,
    "save_morning_report": bind_save_morning_report,
}
AFTER = {
    "get_doc_chunk": after_get_doc_chunk,
    "parse_doc": after_parse_doc,
    "search_doc": after_search_doc,
    "search_report_templates": after_search_report_templates,
    "get_report_template": after_get_report_template,
    "market_snapshot": after_market_snapshot,
    "get_morning_report": after_get_morning_report,
    "save_morning_report": after_save_morning_report,
}


# ── 시장 리포트 요약 (플래너·기존 흐름 공용) ────────────────────

SUMMARY_PROMPT = (
    "은행 직원용 시장 리포트의 요약만 한국어 3문장 이내의 평문으로 작성한다. 양식 목차는 다룰 범위를 알려 주는 참고일 뿐이니 "
    "목차의 제목·항목 이름(예: '주요 환율:')을 쓰지 말고, 제목·목록·굵은 글씨 없이 문장만 쓴다. 선택한 양식의 목적과 항목 범위만 "
    "다루고 양식 전체를 다시 쓰지 않는다. 표의 수치와 단위를 그대로 쓰고 전망·원인 추측·투자 권유를 쓰지 않는다. 기록 재생이면 "
    "실시간이라고 하지 않는다. 실제 기준일이 요청일보다 과거이면 관측일 이후에도 변동이 없었다고 단정하지 않는다."
)


async def market_summary(llm: LLMClient, data: dict, selection: dict | None, *, max_calls: int = 2) -> tuple[str, int]:
    """시장 조회 결과로 3문장 요약을 쓴다. (요약, LLM 호출 수). 검사에 걸리면 한 번 다시 쓰고, 그래도 걸리는 문장은 뺀다."""
    calls = 0
    try:
        body = data["markdown"]
        outline = ""
        if selection:
            template = selection["template"]
            used = template["bindings"]
            values = {key: data[key] for key in ("base_date", "mode")}
            if "fx_table" in used or "data_quality" in used:
                values["fx"] = data["fx"]
            if "rates" in used or "data_quality" in used:
                values["rates"] = data["rates"]
            # 전체 양식은 서버가 보관/렌더링한다. 요약 모델에는 짧은 목차·작성 규칙만 줘 긴 양식도 자르지 않는다.
            outline = ("\n".join(s["text"] for s in template["sections"] if s["type"] == "heading")
                       if "sections" in template else "\n".join(line for line in template["body_markdown"].splitlines() if line.startswith("#")))[:600]
            body = ("사용할 양식:\n" + template["name"] + "\n" + template["description"][:400]
                    + "\n요약 작성 조건:\n" + template.get("summary_instructions", "")
                    + "\n양식 목차:\n" + outline + "\n확인한 수치:\n" + json.dumps(values, ensure_ascii=False))
        prompt = [Message("system", SUMMARY_PROMPT), Message("user", body)]
        reply = (await llm.chat(prompt, [])).message
        calls = 1
        problem = summary_problem(plain_summary(reply.content, outline), data)
        if problem and max_calls > 1:  # 저장 전에 같은 검사를 해 보고, 걸리면 이유를 알려 주고 한 번만 다시 쓰게 한다
            reply = (await llm.chat([*prompt, Message("assistant", reply.content), Message("user", problem)], [])).message
            calls = 2
        summary = plain_summary(reply.content, outline) if len(reply.content) <= 2000 else ""
        if summary and summary_problem(summary, data):
            # 다시 써도 걸리면 걸리는 문장만 빼고, 남은 문장이 검사를 통과할 때만 쓴다
            kept = " ".join(p for p in re.split(r"(?<=[.!?다])\s+", summary) if p and not summary_problem(p, data))
            summary = kept if kept and not summary_problem(kept, data) else summary
        return summary, calls
    except Exception:
        # 시장 수치/표는 모델 없이도 확인 가능하다. 연결 실패 시 지어낸 요약을 사용하지 않는다.
        log.warning("시장 요약 생성 실패, 요약 없이 저장")
        return "", max(calls, 1)


def plain_summary(text: str, outline: str) -> str:
    """요약에서 양식 항목 이름 머리("한눈에 보는 요약:", "**주요 환율**:")와 마크다운 기호를 뗀다 (문장만 남김)."""
    headings = {re.sub(r"^\s*(?:#+\s*)?(?:\d+[.)]\s*)?", "", line).strip() for line in outline.splitlines()} - {""}
    headings |= {"요약", "핵심 요약", "한눈에 보는 요약"}  # 수치 이름(기준금리 등)은 문장의 주어라 남긴다
    text = re.sub(r"[*#`>]+", "", text)
    for heading in sorted(headings, key=len, reverse=True):
        text = re.sub(rf"(?m)(^|(?<=[\s.]))(?:\d+[.)]\s*)?{re.escape(heading)}\s*[:：]\s*", r"\1", text)
    text = re.sub(r"(?m)^\s*[-•]\s*", "", text)
    return re.sub(r"\s*\n\s*", " ", text).strip()


def summary_problem(summary: str, snapshot: dict) -> str:
    """시장 리포트 저장 때와 같은 요약 검사 (market_mcp.service.check_summary). 통과하면 빈 문자열."""
    try:
        from market_mcp.service import check_summary

        check = check_summary(summary, snapshot)
    except Exception:
        return ""
    if check.get("passed"):
        return ""
    reasons = [*(f"표에 없는 숫자 {n}" for n in check.get("numbers_not_in_table", [])), *check.get("forbidden_words", [])]
    return ("방금 요약은 검사를 통과하지 못했다: " + ", ".join(reasons or ["3문장 초과"]) +
            ". 표에 있는 수치만 쓰고, 관측하지 않은 기간의 변동 여부를 단정하지 말고, 3문장 이내 평문으로 다시 써라.")
