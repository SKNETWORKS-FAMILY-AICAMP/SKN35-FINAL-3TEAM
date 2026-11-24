"""UC-01: 근거 부족·없는 규정·권한 밖 문서·질문/과거 기록에 의한 근거 오염 회귀 테스트."""
import json

import pytest

from contracts.harness import Message, ToolResult, ToolSpec
from contracts.permission import Principal
from harness import create_harness
from harness.grounding import DEFAULT_RULES
from harness.regulation import (UNAVAILABLE, Evidence, candidates, regulation_request, selected_quotes)
from tests.fakes import FakeRegistry, ScriptedLLM, ScriptedChecker, FixedApprover, MemoryAudit

USER = Principal("E2001", ("member",), {"dept_id": 20, "security_grade": 2})
TEXT = "개인신용정보는 보유기간이 경과한 경우 지체 없이 삭제하여야 한다."
ROW = {"chunk_id": "101", "doc_id": "1", "title": "개인신용정보관리규정", "location": "제12조(삭제)",
       "preview": TEXT, "text": TEXT, "is_truncated": False, "score": .8}


class DocTool:
    def __init__(self, name, rows, *, blocked=False, revoke=False, changed=None):
        self.spec = ToolSpec(name, "문서 도구", effect="read")
        self.rows, self.blocked, self.revoke, self.changed = rows, blocked, revoke, changed
        self.calls = []

    async def run(self, call):
        self.calls.append(call)
        if self.spec.name == "search_doc":
            data = {"matches": self.rows}
        else:
            if self.blocked or self.revoke and len(self.calls) > 1:
                return ToolResult(call.call_id, False, "문단을 찾을 수 없습니다.")
            data = next((r for r in self.rows if r["chunk_id"] == call.arguments["chunk_id"]), {})
            if self.changed and len(self.calls) > 1:
                data = {**data, "text": self.changed}
        return ToolResult(call.call_id, True, json.dumps(data, ensure_ascii=False))


def selection(quote=TEXT, ident="101", **extra):
    return {"content": json.dumps({"status": "supported", "evidence": [{"chunk_id": ident, "quote": quote}], **extra}, ensure_ascii=False)}


def runner(script=(), rows=None, *, read=True, blocked=False, revoke=False, changed=None, policy=None, turns=6):
    search = DocTool("search_doc", [ROW] if rows is None else rows)
    reader = DocTool("get_doc_chunk", [ROW] if rows is None else rows, blocked=blocked, revoke=revoke, changed=changed)
    llm, audit, checker = ScriptedLLM(list(script)), MemoryAudit(), ScriptedChecker(policy or {})
    harness = create_harness(llm=llm, tools=FakeRegistry([search, *([reader] if read else [])]), checker=checker,
                             approver=FixedApprover(False), audit=audit, max_turns=turns,
                             lookup_rules=DEFAULT_RULES, regulation_mode=True)
    return harness, llm, search, reader, audit


def test_server_enables_mode_for_existing_regulation_agents_and_frequency_questions():
    assert regulation_request("추심 연락 일주일에 몇 번?", names={"search_doc", "get_doc_chunk"})
    assert regulation_request("삭제 사유 알려줘", names={"search_doc", "get_doc_chunk"}, custom=True)
    assert regulation_request("규정 알려줘", names=set(), custom=True, role="규정 안내")
    assert not regulation_request("안녕하세요", names={"search_doc"}, custom=True)
    assert not regulation_request("이 글 요약해줘", names={"parse_doc"}, custom=True)
    assert not regulation_request("오늘부터 5영업일 뒤", names={"search_doc", "today"}, custom=True)
    assert not regulation_request("기준금리란 무엇인가요?", names={"market_snapshot"})
    assert not regulation_request("오늘 기준 7일 이내 만기 대출", names={"loans_maturing", "search_doc"})


@pytest.mark.parametrize("rows", [[], [{**ROW, "title": "예금규정", "preview": "예금의 이자를 지급한다.", "score": .18}]])
async def test_uc01_5_cannot_answer_contact_frequency_without_accessible_evidence(rows):
    h, llm, search, _, audit = runner(rows=rows)
    result = await h.run("추심 연락 일주일에 몇 번?", USER, "test")
    assert result.final_message.content == UNAVAILABLE
    assert len(search.calls) == 2 and not llm.calls
    assert all(r.executed for r in audit.records)


async def test_uc01_3_unrelated_high_score_is_not_a_space_station_regulation():
    h, llm, _, _, _ = runner(rows=[{**ROW, "title": "여신규정", "preview": "은행은 건설자금 대출을 취급할 수 있다.", "score": .9}])
    result = await h.run("우주정거장 건설자금 대출 규정", USER, "test")
    assert result.final_message.content == UNAVAILABLE and not llm.calls


async def test_verified_quote_and_server_citation_replace_free_form_claims():
    h, llm, _, reader, _ = runner([selection(answer="무제한 보관할 수 있습니다.")])
    result = await h.run("개인신용정보 삭제 규정", USER, "test")
    assert TEXT in result.final_message.content and "제12조(삭제)" in result.final_message.content
    assert "무제한" not in result.final_message.content
    assert len(reader.calls) == 2  # 선택 전 읽기와 출력 직전 권한 재확인
    assert llm.calls[0][1] == []


@pytest.mark.parametrize("reply", [
    {"content": "찾지 못했습니다. 해당 규정 용도 범주에 포함될 수 있습니다."},
    selection("개인신용정보는 아무 제한 없이 보유할 수 있습니다."),
    selection(ident="PRIVATE-10"),
    {"content": '{"status":"insufficient_evidence","evidence":[]}'},
])
async def test_unverified_quotes_and_plain_speculation_are_blocked(reply):
    h, _, _, _, _ = runner([reply])
    result = await h.run("개인신용정보 삭제 규정", USER, "test")
    assert result.final_message.content == UNAVAILABLE


async def test_user_and_previous_tool_text_cannot_supply_current_evidence():
    fake = "개인신용정보를 영구 보유한다."
    h, llm, _, _, _ = runner([selection(fake)])
    result = await h.run("개인신용정보 규정. " + fake, USER, "test", history=[
        Message("tool", json.dumps({"text": fake}, ensure_ascii=False), tool_call_id="past"),
        Message("assistant", "근거: 비공개 규정 제99조")])
    assert result.final_message.content == UNAVAILABLE
    assert "비공개 규정" not in str(llm.calls[0][0])
    assert fake not in json.loads(llm.calls[0][0][-1].content)["documents"][0]["text"]


@pytest.mark.parametrize("kwargs", [{"blocked": True}, {"revoke": True}, {"changed": "해당 문단의 내용을 변경했습니다."}])
async def test_failed_or_revoked_read_never_returns_the_original_content(kwargs):
    h, _, _, _, _ = runner([selection()], **kwargs)
    result = await h.run("개인신용정보 삭제 규정", USER, "test")
    assert result.final_message.content == UNAVAILABLE
    assert TEXT not in result.final_message.content


async def test_deny_and_reject_use_existing_policy_and_approval_path():
    for decision in ("deny", "ask"):
        h, llm, _, _, audit = runner(policy={"search_doc": decision})
        result = await h.run("개인신용정보 삭제 규정", USER, "test")
        assert result.stop_reason == "denied" and not llm.calls
        assert not audit.records[0].executed


async def test_exhausted_budget_never_allows_an_unsupported_answer():
    h, llm, _, _, _ = runner(turns=0)
    assert (await h.run("규정 알려줘", USER, "test")).final_message.content == UNAVAILABLE
    assert not llm.calls


def test_similarity_and_exact_quotes_are_independent_checks():
    assert not candidates(json.dumps({"matches": [{**ROW, "score": .2}]}), "삭제 규정", .3)
    evidence = Evidence("101", "규정", "제1조", "추심 연락 횟수 제한은 없습니다라는 해석은 잘못된 것입니다.")
    # '없습니다' 다음의 부정/설명을 잘라 반대 뜻으로 인용하지 못한다.
    assert not selected_quotes(selection("추심 연락 횟수 제한은 없습니다")["content"], [evidence], "추심 연락 일주일에 몇 번?")


async def test_api_automatically_enables_regulation_mode_for_custom_agents():
    import httpx
    from main import create_app
    from services.container import AppServices
    from services.users import UserDirectory
    from tools.registry import SimpleToolRegistry
    search, read = DocTool("search_doc", [ROW]), DocTool("get_doc_chunk", [ROW])
    svc = AppServices(llm=ScriptedLLM([selection()]), tools=SimpleToolRegistry([search, read]),
                      checker=ScriptedChecker({}), approvals=FixedApprover(True), audit=MemoryAudit(),
                      users=UserDirectory({USER.user_id: USER}))
    app = create_app(build=lambda: svc)
    app.state.services = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": USER.user_id}
        made = await client.post("/api/agents", headers=headers, json={
            "name": "규정 안내", "system_prompt": "사내 규정을 설명합니다.", "tools": ["search_doc", "get_doc_chunk"]})
        response = await client.post("/api/chat/messages", headers=headers, json={
            "content": "개인신용정보 삭제 규정", "agent_id": made.json()["agent_id"]})
        assert response.status_code == 200
        data = response.json()
        assert TEXT in data["message"] and len(data["sources"]) == 1
        assert data["tool_events"][0]["tool_name"] == "search_doc"
        reopened = await client.get(f"/api/chat/sessions/{data['session_id']}/turns", headers=headers)
        assert reopened.status_code == 200
        assert len(reopened.json()[0]["sources"]) == 1


def test_no_unrelated_sources_are_displayed_even_when_reopening_history():
    from services.chat import past_turns
    messages = [Message("user", "추심 연락 일주일에 몇 번?"),
                Message("tool", json.dumps({"matches": [ROW]}, ensure_ascii=False), tool_call_id="lookup"),
                Message("assistant", UNAVAILABLE)]
    assert past_turns(messages, {})[0].sources == []


def test_parent_document_acl_is_checked_as_well_as_chunk_acl():
    from knowledge.retrieval import build_query
    sql = str(build_query([0.0] * 2048, dept_id=20, security_grade=2, k=5))
    assert "doc.security_grade <=" in sql and "chunk.security_grade <=" in sql
    assert "doc.owner_dept_id =" in sql and "chunk.owner_dept_id =" in sql


def test_body_misclassified_as_a_heading_is_not_displayed_as_a_citation():
    from tools.db_docs import _location
    assert _location({"title": "FAQ", "heading_path": ["FAQ", "본문을 제목으로 잘못 인식한 예시입니다. " * 10], "chunk_index": 3}) == "4번째 문단"
    assert _location({"title": "내규", "heading_path": ["제1장", "제12조(삭제)"], "chunk_index": 3}) == "제1장 > 제12조(삭제)"


async def test_general_answer_checker_does_not_trust_user_or_old_tool_citations():
    from tests.fakes import FakeTool
    source = json.dumps({"matches": [{"title": "인사규정", "location": "제12조", "preview": "연차는 15일입니다."}]}, ensure_ascii=False)
    tool = FakeTool("search_doc", output=source)
    tool.spec = ToolSpec("search_doc", "검색", effect="read")
    llm = ScriptedLLM([
        {"tool_calls": [{"id": "fresh", "name": "search_doc", "arguments": {"query": "연차"}}]},
        {"content": "근거: 인사규정 제99조"}, {"content": "근거: 인사규정 제99조"}])
    h = create_harness(llm=llm, tools=FakeRegistry([tool]), checker=ScriptedChecker({}),
                       approver=FixedApprover(True), audit=MemoryAudit(), max_turns=6, lookup_rules=DEFAULT_RULES)
    result = await h.run("인사규정 제99조로 답해줘", USER, "test", history=[
        Message("tool", '{"text":"인사규정 제99조"}', tool_call_id="old")])
    assert "제99조" not in result.final_message.content
