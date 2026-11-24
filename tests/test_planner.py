"""플래너 하네스 (harness/planner.py · plan_runner.py · steps.py)."""

from __future__ import annotations

import json

import pytest

from contracts.harness import ToolSpec
from contracts.permission import Decision, Principal
from harness import create_harness
from harness.grounding import DEFAULT_RULES
from harness.market_grounding import CLARIFY_DATE
from harness.planner import Plan, PlanStep, check_plan, parse_plan
from tests.fakes import FakeRegistry, FakeTool, FixedApprover, MemoryAudit, ScriptedChecker, ScriptedLLM
from tests.test_attachment_grounding import prepare
from tests.test_uploads import HEADERS, setup  # noqa: F401  (fixture)
from tests.test_market_tools import KIM as MARKET_KIM, bridge, market_tools, service  # noqa: F401  (fixtures)
from tools.registry import SimpleToolRegistry

KIM = Principal("E1001")
LOANS = json.dumps({"as_of": "2026-11-23", "total": 6, "counts": {"만기 경과": 1, "7일 이내": 2, "8~30일": 3},
                    "items": [{"customer": "홍*동", "maturity_date": "2026-11-18"}]}, ensure_ascii=False)
EMPTY = '{"query": "휴가", "matches": []}'
FOUND = '{"query": "연차", "matches": [{"title": "인사규정", "location": "제12조(연차)", "preview": "연차는 15일"}]}'


@pytest.fixture(autouse=True)
def answer_log(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSWER_CHECK_LOG_PATH", str(tmp_path / "answer_checks.jsonl"))


def tool(name, output="ok", effect="read", log=None):
    fake = FakeTool(name, output=output, log=log)
    fake.spec = ToolSpec(name=name, description=f"fake {name}", effect=effect)
    return fake


def spec(name, effect="read"):
    return ToolSpec(name=name, description=name, effect=effect)


def plan_reply(*tools):
    return {"content": json.dumps({"steps": [{"tool": t, "goal": f"{t} 단계"} for t in tools]})}


def harness(script, tools, max_turns=6, checker=None):
    llm = ScriptedLLM(script)
    return create_harness(llm=llm, tools=FakeRegistry(tools), checker=checker or ScriptedChecker({}),
                          approver=FixedApprover(True), audit=MemoryAudit(), max_turns=max_turns,
                          lookup_rules=DEFAULT_RULES, planner=True), llm


# ── 계획 읽기·검사 ────────────────────────────────────────

def test_parse_plan_reads_json_inside_text_and_rejects_garbage():
    text = '계획입니다\n```json\n{"steps": [{"tool": "search_doc", "goal": "찾기"}, "fill_document"]}\n```'
    assert [s.tool for s in parse_plan(text)] == ["search_doc", "fill_document"]
    assert parse_plan("모르겠어요") is None and parse_plan('{"answer": 1}') is None
    assert parse_plan('{"steps": []}') == []


def check(tools, question, specs):
    return check_plan(Plan([PlanStep(t) for t in tools]), question, specs, DEFAULT_RULES, limit=6)


def test_check_plan_drops_unknown_duplicate_and_unrequested_write_steps():
    specs = [spec("loans_maturing"), spec("send_maturity_alert", "write")]
    plan = check(["loans_maturing", "loans_maturing", "nope", "send_maturity_alert"], "만기 대출 몇 건이야?", specs)
    assert plan.tools == ["loans_maturing"] and len(plan.notes) == 3


def test_check_plan_adds_missing_lookup_from_rules():
    specs = [spec("search_doc"), spec("get_doc_chunk")]
    plan = check([], "연차 규정 알려줘", specs)
    assert plan.tools == ["search_doc"] and plan.steps[0].added_by == "rule"


def test_check_plan_puts_required_steps_first():
    specs = [spec(n) for n in ("search_report_templates", "get_report_template", "market_snapshot")] + [
        spec("save_morning_report", "write")]
    plan = check(["save_morning_report"], "2026-11-18 금리·환율 리포트 만들어줘", specs)
    assert plan.tools == ["search_report_templates", "get_report_template", "market_snapshot", "save_morning_report"]
    assert {s.added_by for s in plan.steps[:3]} == {"requires"}
    # 조회만 물으면 저장 단계는 빠진다
    assert check(["market_snapshot", "save_morning_report"], "2026-11-18 환율 조회", specs).tools == ["market_snapshot"]
    # 리포트를 만들어 달라는데 계획에 저장이 없으면 넣는다
    assert check(["market_snapshot"], "금리·환율 리포트 만들어줘", specs).tools[-1] == "save_morning_report"


def test_notice_mail_is_sent_only_after_the_list_was_shown():
    specs = [spec("prepare_maturity_notices"), spec("send_maturity_notice", "send")]
    first = check(["prepare_maturity_notices", "send_maturity_notice"], "7일 이내 만기 고객에게 안내 메일 보내 줘", specs)
    assert first.tools == ["prepare_maturity_notices"]
    assert check(["send_maturity_notice"], "응 보내 줘", specs).tools == ["send_maturity_notice"]


def test_mail_send_step_only_for_send_requests_not_drafts():
    specs = [spec("search_doc"), spec("send_mail", "send")]
    assert check(["send_mail"], "팀장님께 보낼 휴가 신청 메일 초안 작성해 줘", specs).tools == []
    assert check(["send_mail"], "휴가 신청 메일 작성해 줘", specs).tools == []  # 보내 달라는 말이 없음
    assert check(["send_mail"], "박종원 팀장에게 회의 일정 메일 보내 줘", specs).tools == ["send_mail"]
    assert check([], "박종원 팀장에게 회의 일정 메일 보내 줘", specs).tools == ["send_mail"]  # 플래너가 빠뜨려도 넣음


DIRECTORY = [{"user_id": "E1002", "name": "박종원", "email": "park@example.com"},
             {"user_id": "E2001", "name": "나치훈", "email": "na@example.com"}]


class MailTool:
    def __init__(self):
        self.spec = ToolSpec("send_mail", "메일 보내기", effect="send", display_name="메일 보내기")
        self.calls = []

    async def run(self, call):
        from contracts.harness import ToolResult

        self.calls.append(call)
        return ToolResult(call.call_id, True, json.dumps({"status": "simulated"}))


def mail_runner(script, directory=DIRECTORY, approve=True):
    mail = MailTool()
    llm = ScriptedLLM(script)

    async def recipients(principal):
        return directory

    runner = create_harness(llm=llm, tools=FakeRegistry([mail]), checker=ScriptedChecker({"send_mail": Decision.ASK}),
                            approver=FixedApprover(approve), audit=MemoryAudit(), max_turns=6,
                            lookup_rules=DEFAULT_RULES, planner=True, mail_recipients=recipients)
    return runner, llm, mail


async def test_mail_recipient_comes_from_directory_not_from_the_model():
    runner, llm, mail = mail_runner([
        plan_reply("send_mail"),
        {"tool_calls": [{"id": "m1", "name": "send_mail", "arguments": {
            "to": ["someone@evil.example"], "cc": ["x@evil.example"], "subject": "회의 일정", "body": "내일 10시 회의입니다."}}]},
    ])
    result = await runner.run("박종원 팀장에게 내일 10시 회의 일정 메일 보내 줘", KIM, "s")
    sent = mail.calls[0].arguments
    assert sent["to"] == ["park@example.com"] and sent["cc"] == [] and sent["subject"] == "회의 일정"
    assert "박종원" in llm.calls[1][0][-1].content  # 단계 안내에 받는 사람을 알려 줌
    assert result.final_message.content.startswith("가상 메일 테스트가 완료되었습니다")


async def test_mail_without_named_recipient_is_not_sent_and_draft_is_answered():
    runner, llm, mail = mail_runner([
        plan_reply("send_mail"),
        {"content": "제목: 휴가 신청\n\n12월 1일 연차를 신청합니다."},
    ])
    result = await runner.run("휴가 신청 메일 보내 줘", KIM, "s")
    assert not mail.calls and len(llm.calls) == 2
    text = result.final_message.content
    assert text.startswith("제목: 휴가 신청") and "받는 사람을 찾지 못해 보내지 않았습니다" in text


async def test_mail_to_ambiguous_name_is_not_sent():
    twins = [*DIRECTORY, {"user_id": "E3001", "name": "박종원", "email": "park2@example.com"}]
    runner, _, mail = mail_runner([plan_reply("send_mail"), {"content": "제목: 회의\n\n회의 안내입니다."}], directory=twins)
    result = await runner.run("박종원에게 회의 메일 보내 줘", KIM, "s")
    assert not mail.calls and "여러 명" in result.final_message.content


async def test_rejected_mail_approval_stops_without_sending():
    runner, _, mail = mail_runner([
        plan_reply("send_mail"),
        {"tool_calls": [{"id": "m1", "name": "send_mail", "arguments": {"to": [], "subject": "회의", "body": "안내"}}]},
    ], approve=False)
    result = await runner.run("나치훈 대리에게 회의 메일 보내 줘", KIM, "s")
    assert result.stop_reason == "denied" and not mail.calls


async def test_mail_draft_request_writes_a_draft_without_sending():
    log = []
    runner, llm = harness([plan_reply("send_mail"), {"content": "제목: 휴가 신청\n\n팀장님, 12월 1일 연차를 신청합니다."}],
                          [tool("send_mail", '{"status": "sent"}', "send", log)])
    result = await runner.run("팀장님께 보낼 휴가 신청 메일 초안 써 줘", KIM, "s")
    assert not log and result.final_message.content.startswith("제목: 휴가 신청")
    assert llm.calls[1][1] == []  # 답 쓰기에는 발송 도구를 주지 않는다


# ── 규정 안내 ─────────────────────────────────────────────

async def test_regulation_runs_planned_search_with_only_that_tool_then_answers_without_tools():
    log = []
    runner, llm = harness([
        plan_reply("search_doc"),
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "연차"}}]},
        {"content": "연차는 15일입니다.\n근거: 인사규정 제12조(연차)"},
    ], [tool("search_doc", FOUND, log=log), tool("get_doc_chunk", log=log)])
    result = await runner.run("연차 규정 알려줘", KIM, "s")
    assert log == ["search_doc"] and result.stop_reason == "completed"
    assert [t.name for t in llm.calls[1][1]] == ["search_doc"]  # 단계에는 그 툴 하나만
    assert llm.calls[2][1] == []  # 답 쓰기에는 툴을 주지 않는다
    assert result.final_message.content.startswith("연차는 15일") and result.llm_calls == 3


async def test_unreadable_plan_falls_back_to_rules_and_empty_search_retries_once():
    log = []
    runner, llm = harness([
        {"content": "음... 계획을 못 쓰겠어요"},  # JSON 아님 → 규칙으로 계획
        {"content": "검색 안 할래요"},  # 단계에서도 툴을 안 부름 → 규칙의 인자(질문 핵심 단어)로
    ], [tool("search_doc", EMPTY, log=log)])
    result = await runner.run("휴가 규정 알려줘", KIM, "s")
    assert log == ["search_doc", "search_doc"]  # 0건이라 검색어를 바꿔 한 번 더
    queries = [m.tool_calls[0].arguments["query"] for m in result.messages if m.tool_calls]
    assert len(set(queries)) == 2
    assert result.final_message.content == "등록된 규정에서 근거를 찾지 못했습니다." and len(llm.calls) == 2


# ── 만기 대출 정리표 ───────────────────────────────────────

async def test_maturity_report_steps_use_rule_arguments_when_model_skips_the_tool():
    log = []
    runner, llm = harness([
        plan_reply("loans_maturing", "fill_document"),
        {"tool_calls": [{"id": "l1", "name": "loans_maturing", "arguments": {"days": 30}}]},
        {"content": "정리표는 곧 만들겠습니다."},  # 문서 단계에서 툴을 안 부름
        {"content": "30일 이내 만기 대출 6건으로 정리표를 만들었습니다. 화면의 PDF·DOCX 버튼으로 받으세요."},
    ], [tool("loans_maturing", LOANS, log=log), tool("fill_document", '{"created": true}', "write", log)])
    result = await runner.run("30일 이내 만기 대출 정리표 만들어줘", KIM, "s")
    assert log == ["loans_maturing", "fill_document"]
    made = [m for m in result.messages if m.tool_calls][-1].tool_calls[0]
    assert made.arguments == {"form_id": "maturity-report", "params": {"days": 30}}
    assert [t.name for t in llm.calls[2][1]] == ["fill_document"]
    assert "6건" in result.final_message.content


async def test_denied_step_stops_the_plan_with_server_text():
    log = []
    runner, llm = harness([
        plan_reply("loans_maturing", "fill_document"),
        {"tool_calls": [{"id": "l1", "name": "loans_maturing", "arguments": {"days": 30}}]},
        {"tool_calls": [{"id": "f1", "name": "fill_document", "arguments": {"form_id": "maturity-report"}}]},
    ], [tool("loans_maturing", LOANS, log=log), tool("fill_document", "", "write", log)],
        checker=ScriptedChecker({"fill_document": Decision.DENY}))
    result = await runner.run("만기 대출 정리표 만들어줘", KIM, "s")
    assert result.stop_reason == "denied" and log == ["loans_maturing"] and len(llm.calls) == 3


# ── 금리·환율 리포트 (실제 시장 툴) ─────────────────────────

def market_harness(script, service, bridge, names=None):  # noqa: F811
    llm = ScriptedLLM(script)
    tools = market_tools(service, bridge, *([names] if names else []))
    return create_harness(llm=llm, tools=SimpleToolRegistry(tools), checker=ScriptedChecker({}),
                          approver=FixedApprover(True), audit=MemoryAudit(), max_turns=6, planner=True), llm


async def test_market_report_plan_fills_template_snapshot_and_save_from_results(service, bridge):  # noqa: F811
    runner, llm = market_harness([
        plan_reply("market_snapshot", "save_morning_report"),
        {"content": "USD는 1,338.80원입니다. 기준금리는 3.00%입니다."},  # 요약
    ], service, bridge)
    result = await runner.run("2026-11-18 금리·환율 리포트 만들어줘", MARKET_KIM, "s")
    called = [m.tool_calls[0].tool_name for m in result.messages if m.tool_calls]
    assert called == ["market_snapshot", "search_report_templates", "get_report_template", "save_morning_report"]
    saved = json.loads(result.messages[-2].content)
    assert result.final_message.content == saved["text"] and "1,338.80" in saved["text"]
    assert len(llm.calls) == 2  # 계획 + 요약 (인자는 모두 코드가 앞 단계 결과에서 채움)


async def test_market_lookup_only_shows_server_table_after_plan(service, bridge):  # noqa: F811
    runner, llm = market_harness([plan_reply("market_snapshot")], service, bridge)
    result = await runner.run("2026-11-18 환율 조회", MARKET_KIM, "s")
    assert "1,338.80" in result.final_message.content and len(llm.calls) == 1


async def test_market_relative_date_is_asked_not_guessed(service, bridge):  # noqa: F811
    runner, _ = market_harness([plan_reply("market_snapshot", "save_morning_report")], service, bridge)
    result = await runner.run("지난주 금요일 금리·환율 리포트 만들어줘", MARKET_KIM, "s")
    assert result.final_message.content == CLARIFY_DATE
    assert not any(m.tool_calls for m in result.messages)


# ── 첨부 문서 ─────────────────────────────────────────────

def test_check_plan_reads_attachment_first_only_when_it_is_referred_to():
    specs = [spec("parse_doc"), spec("search_doc")]
    old = [{"file_id": "f1", "filename": "규정.hwpx", "new": False}]
    assert check(["parse_doc"], "안녕", specs).tools == []  # 첨부가 없으면 읽을 것도 없다
    plan = check_plan(Plan([PlanStep("search_doc")]), "이 문서 내용이 내규에 맞아?", specs, DEFAULT_RULES, limit=6, files=old)
    assert plan.tools == ["parse_doc", "search_doc"]
    later = check_plan(Plan([]), "오늘 날씨 어때", specs, DEFAULT_RULES, limit=6, files=old)
    assert later.tools == []  # 지난 턴의 첨부를 가리키지 않으면 읽지 않는다
    new = [{**old[0], "new": True}]
    assert check_plan(Plan([]), "핵심만 알려줘", specs, DEFAULT_RULES, limit=6, files=new).tools == ["parse_doc"]


def test_attachment_does_not_replace_corporate_search_when_rules_are_asked():
    specs = [spec("parse_doc"), spec("search_doc")]
    new = [{"file_id": "f1", "filename": "신청서.pdf", "new": True}]
    compare = check_plan(Plan([]), "첨부한 신청서가 우리 서류 보완 규정에 맞는지 봐 줘", specs, DEFAULT_RULES, limit=6, files=new)
    assert compare.tools == ["parse_doc", "search_doc"]
    only = check_plan(Plan([]), "이 문서에서 보완 기한 며칠이야?", specs, DEFAULT_RULES, limit=6, files=new)
    assert only.tools == ["parse_doc"]
    # 첨부를 가리키지 않는 규정 질문: 플래너가 지난 첨부를 읽겠다고 해도 사내 검색은 한다
    old = [{**new[0], "new": False}]
    rule = check_plan(Plan([PlanStep("parse_doc")]), "서류 반려 사유는 어떻게 적어?", specs, DEFAULT_RULES, limit=6, files=old)
    assert rule.tools == ["parse_doc", "search_doc"]


async def test_slow_planner_falls_back_to_rules(monkeypatch):
    import asyncio

    import harness.planner as planner

    class Slow(ScriptedLLM):
        async def chat(self, messages, tools):
            if not self.calls:
                self.calls.append((messages, tools))
                await asyncio.sleep(1)
            return await super().chat(messages, tools)

    monkeypatch.setattr(planner, "PLANNER_TIMEOUT", 0.01)
    log = []
    llm = Slow([{}, {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "연차"}}]},
                {"content": "연차는 15일입니다.\n근거: 인사규정 제12조(연차)"}])
    runner = create_harness(llm=llm, tools=FakeRegistry([tool("search_doc", FOUND, log=log)]), checker=ScriptedChecker({}),
                            approver=FixedApprover(True), audit=MemoryAudit(), max_turns=6, lookup_rules=DEFAULT_RULES,
                            planner=True)
    result = await runner.run("연차 규정 알려줘", KIM, "s")
    assert log == ["search_doc"] and result.final_message.content.startswith("연차는 15일")


async def ask(setup, script, content):  # noqa: F811
    svc, item, client, searched = await prepare(setup, script)
    svc.use_planner = True
    (await svc.tools.get("search_doc")).spec = spec("search_doc")  # 가짜 툴 기본값(write) 대신 실제처럼 조회
    async with client:
        body = (await client.post("/api/chat/messages", headers=HEADERS,
                                  json={"content": content, "file_ids": [item["file_id"]]})).json()
    return svc, body, searched


async def test_attachment_question_is_planned_read_by_server_then_answered_without_tools(setup):  # noqa: F811
    svc, body, searched = await ask(setup, [
        plan_reply("parse_doc"),
        {"content": "보존 기간은 7년입니다. 근거: 규정.hwpx 제2조"},
    ], "첨부 문서의 보존 기간은?")
    assert "7년" in body["message"] and [e["tool_name"] for e in body["tool_events"]] == ["parse_doc"]
    assert svc.llm.calls[1][1] == [] and len(svc.llm.calls) == 2  # 파일·인자는 서버가 정해 단계 LLM 호출 없음
    assert "규정.hwpx" in svc.llm.calls[0][0][1].content  # 플래너는 첨부 파일 이름을 본다
    assert not searched


async def test_attachment_plus_corporate_search_runs_both_steps(setup):  # noqa: F811
    svc, body, searched = await ask(setup, [
        plan_reply("parse_doc", "search_doc"),
        {"tool_calls": [{"id": "s1", "name": "search_doc", "arguments": {"query": "보존 기간"}}]},
        {"content": "첨부 문서의 보존 기간은 7년입니다."},
    ], "첨부 문서의 보존 기간이 사내 규정과 같은지 찾아 줘")
    assert [e["tool_name"] for e in body["tool_events"]] == ["parse_doc", "search_doc"] and searched == ["search_doc"]
    assert "7년" in body["message"]


class PagedParser:
    """첫 읽기에는 답이 없고 남은 부분이 있다고 알려 주는 첨부 파서."""

    def __init__(self):
        self.spec = ToolSpec("parse_doc", "첨부 읽기", effect="read")
        self.offsets = []

    async def run(self, call):
        from contracts.harness import ToolResult

        offset = call.arguments.get("offset", 0)
        self.offsets.append(offset)
        first = offset == 0
        payload = {"file_id": call.arguments["file_id"],
                   "text": "1. 일반 사항\n사내 업무 자료를 관리한다." if first else "제2조(보존 기간) 서류 보존 기간은 7년이다.",
                   "read_status": {"document_complete": False, "has_more": first, "next_offset": 3 if first else None}}
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False))


async def test_attachment_answer_reads_the_rest_once_when_the_first_part_lacks_evidence():
    parser = PagedParser()
    llm = ScriptedLLM([plan_reply("parse_doc"), {"content": "보존 기간은 7년입니다."}, {"content": "보존 기간은 7년입니다."}])
    runner = create_harness(llm=llm, tools=FakeRegistry([parser]), checker=ScriptedChecker({}), approver=FixedApprover(True),
                            audit=MemoryAudit(), max_turns=6, lookup_rules=DEFAULT_RULES, planner=True)
    result = await runner.run("첨부 문서의 보존 기간은?", KIM, "s",
                              attachments=[{"file_id": "f1", "filename": "규정.pdf", "new": True}])
    assert parser.offsets == [0, 3]  # 첫 답이 근거 검사에 걸려 남은 부분을 이어 읽음
    assert result.final_message.content == "보존 기간은 7년입니다." and len(llm.calls) == 3


# ── 규정 모드 (dy UC-1 수정) ───────────────────────────────

def regulation_runner(script, rows=None):
    from tests.test_regulation_evidence import ROW, DocTool

    search = DocTool("search_doc", [ROW] if rows is None else rows)
    reader = DocTool("get_doc_chunk", [ROW] if rows is None else rows)
    llm = ScriptedLLM(list(script))
    runner = create_harness(llm=llm, tools=FakeRegistry([search, reader]), checker=ScriptedChecker({}),
                            approver=FixedApprover(True), audit=MemoryAudit(), max_turns=6, lookup_rules=DEFAULT_RULES,
                            planner=True, regulation_mode=True)
    return runner, llm, search, reader


async def test_planned_regulation_answer_is_a_verified_quote():
    from tests.test_regulation_evidence import TEXT, selection

    runner, llm, search, reader = regulation_runner([plan_reply("search_doc"), selection(answer="무제한 보관 가능")])
    result = await runner.run("개인신용정보 삭제 규정", KIM, "s")
    text = result.final_message.content
    assert text.startswith("접근 가능한 규정에서 확인한 원문입니다.") and TEXT in text and "무제한" not in text
    assert search.calls[0].arguments == {"query": "개인신용정보 삭제 규정"}  # 검색어는 코드가 정함
    assert len(reader.calls) == 2 and len(llm.calls) == 2  # 원문 읽기 + 고른 인용 재확인 / 계획 + 인용 선택


async def test_planned_regulation_without_relevant_evidence_says_unavailable():
    from harness.regulation import UNAVAILABLE
    from tests.test_regulation_evidence import ROW

    low = [{**ROW, "title": "예금규정", "preview": "예금의 이자를 지급한다.", "score": .18}]
    runner, llm, search, reader = regulation_runner([plan_reply("search_doc", "get_doc_chunk")], rows=low)
    result = await runner.run("추심 연락 일주일에 몇 번?", KIM, "s")
    assert result.final_message.content == UNAVAILABLE
    assert len(search.calls) == 2 and not reader.calls and len(llm.calls) == 1  # 재검색까지 후보 없음 → 모델 답 없음


async def test_planned_regulation_rejects_free_form_answer():
    from harness.regulation import UNAVAILABLE

    runner, _, _, _ = regulation_runner([plan_reply("search_doc"), {"content": "보유기간이 지나도 보관할 수 있습니다."}])
    result = await runner.run("개인신용정보 삭제 규정", KIM, "s")
    assert result.final_message.content == UNAVAILABLE


async def test_unusable_attachment_answer_becomes_real_excerpt(setup):  # noqa: F811
    _, body, _ = await ask(setup, [{"content": "계획 없음"}] + [{"content": "보존 기간은 5년입니다."}] * 2,
                           "첨부 문서의 보존 기간은?")
    assert "7년" in body["message"] and "발췌" in body["message"] and "5년" not in body["message"]
