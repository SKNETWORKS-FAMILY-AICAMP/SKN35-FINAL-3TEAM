"""UC-03 만기 알림, UC-05 고객 메일 수신자 검사."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from contracts.permission import Decision, Principal, ToolCall
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.customer_mail import CustomerMailRule
from permission.policy import load_policy
from services.container import AppServices
from services.customer_notices import NoticeLog
from services.inbox import InboxStore
from services.users import UserDirectory
from harness import create_harness
from tests.fakes import FixedApprover, MemoryAudit, ScriptedLLM
from tools.loans import LoansMaturingTool, LoanStore, mask_name, won
from tools.maturity_alert import SendMaturityAlertTool
from contracts.harness import ToolResult
from tools.maturity_notice import PrepareMaturityNoticesTool, SendMaturityNoticeTool, render_notice
from tools.number_check import unknown_numbers
from tools.registry import SimpleToolRegistry

ROOT = Path(__file__).resolve().parents[1]
KIM = Principal("E1001", ("member",), {"dept_id": 10, "security_grade": 2})
PARK = Principal("E2001", ("member",), {"dept_id": 20, "security_grade": 2})
LEE = Principal("E1002", ("dept_head",), {"dept_id": 10, "security_grade": 3})
ADMIN = Principal("E0001", ("E0001",), {"dept_id": 1, "security_grade": 5})


@pytest.fixture
def loans():
    return LoanStore(ROOT / "data/fixtures/loans_demo.json")


@pytest.fixture
def inbox(tmp_path):
    return InboxStore(tmp_path / "inbox.sqlite3")


def call(name, arguments, principal=KIM):
    return ToolCall("c1", name, arguments, principal, "session")


def test_masking_and_amounts():
    assert mask_name("홍길동", "individual") == "홍*동"
    assert mask_name("김철", "individual") == "김*"
    assert mask_name("남궁민수", "individual") == "남**수"
    assert mask_name("(주)한빛정밀", "corporate") == "(주)한빛정밀"
    assert won(150_000_000) == "1억 5,000만원" and won(30_000_000) == "3,000만원" and won(500_000_000) == "5억원"


async def test_maturing_buckets_only_my_loans(loans):
    result = await LoansMaturingTool(loans).run(call("loans_maturing", {"days": 30}))
    data = result.data
    assert result.ok and data["as_of"] == "2026-11-23"
    assert data["counts"] == {"만기 경과": 1, "7일 이내": 2, "8~30일": 3}
    ids = [item["loan_id"] for item in data["items"]]
    assert ids == ["L-1001", "L-1002", "L-1003", "L-1004", "L-1005", "L-1006"]  # L-1007(53일)은 빠진다
    assert all(item["loan_id"].startswith("L-1") for item in data["items"])  # park 담당은 안 보인다
    assert data["first_look"] == ["L-1001", "L-1002", "L-1003"]  # 경과 → 조기경보 → 가까운 순
    assert "홍길동" not in result.content and "홍*동" in result.content
    assert "조기경보" in data["items"][1]["follow_up"]


async def test_window_boundary_and_no_officer(loans):
    tool = LoansMaturingTool(loans)
    week = (await tool.run(call("loans_maturing", {"days": 7}))).data
    assert [item["loan_id"] for item in week["items"]] == ["L-1001", "L-1002", "L-1003"]  # 7일째(11-30)까지 포함
    none = await tool.run(call("loans_maturing", {}, LEE))
    assert none.ok and none.data["message"] == "담당 고객이 없습니다." and none.data["items"] == []


async def test_no_way_to_pick_another_officer(loans):
    tool = LoansMaturingTool(loans)
    for arguments in ({"officer_id": "E2001"}, {"days": 0}, {"days": 91}, {"days": "30"}):
        assert not (await tool.run(call("loans_maturing", arguments))).ok


def test_as_of_override(loans):
    store = LoanStore(loans.path, as_of=date(2026, 12, 1))
    data = store.maturing(KIM, 7)
    assert data["as_of"] == "2026-12-01" and data["counts"]["만기 경과"] == 3


def test_unknown_numbers():
    source = json.dumps({"total": 6, "date": "2026-11-26", "balance": "1억 5,000만원"}, ensure_ascii=False)
    assert unknown_numbers("만기 경과 1건과 11월 26일 만기 건을 먼저 보세요. 6건입니다", source, "1") == set()
    assert unknown_numbers("1억 5,000만원 건", source) == set()
    assert unknown_numbers("12건을 확인하세요", source) == {"12"}


async def test_alert_rebuilds_list_and_drops_bad_summary(loans, inbox):
    tool = SendMaturityAlertTool(loans, inbox)
    ok = await tool.run(call("send_maturity_alert", {"days": 30, "summary": "만기 경과 1건(홍*동)과 11월 26일 만기인 한빛정밀 건을 오늘 먼저 확인하세요."}))
    assert ok.ok and not ok.data["summary_dropped"] and ok.data["total"] == 6
    # 같은 기준일·기간 알림은 다시 만들지 않고 있던 알림을 알려 준다
    again = await tool.run(call("send_maturity_alert", {"days": 30, "summary": "다시 보내 주세요."}))
    assert again.ok and again.data["already_sent"] and again.data["alert_id"] == ok.data["alert_id"]
    assert len(await inbox.list("E1001")) == 1
    bad = await tool.run(call("send_maturity_alert", {"days": 60, "summary": "총 47건 중 9건이 급합니다."}))
    assert bad.ok and bad.data["summary_dropped"] and "47, 9" in bad.data["message"]
    stored = await inbox.open("E1001", bad.data["alert_id"])
    assert stored["summary"] == "" and stored["read_at"] and len(stored["payload"]["items"]) == 7  # 60일 이내 7건
    assert await inbox.unread_count("E1001") == 1
    assert await inbox.open("E2001", bad.data["alert_id"]) is None and not await inbox.delete("E2001", bad.data["alert_id"])
    assert await inbox.delete("E1001", bad.data["alert_id"]) and await inbox.open("E1001", bad.data["alert_id"]) is None
    # 모델이 목록을 넘겨도 받지 않는다
    assert not (await tool.run(call("send_maturity_alert", {"days": 30, "items": []}))).ok
    no_loans = await tool.run(call("send_maturity_alert", {"days": 30}, LEE))
    assert no_loans.ok and not no_loans.data["sent"] and (await inbox.list("E1002")) == []


async def test_inbox_api_only_shows_own_alerts(loans, inbox, tmp_path):
    alert = await SendMaturityAlertTool(loans, inbox).run(call("send_maturity_alert", {"days": 30}))
    svc = AppServices(
        llm=ScriptedLLM([]), tools=SimpleToolRegistry([]), checker=create_checker(rules=[], default=Decision.ASK),
        approvals=FutureApprovalHandler(1), audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory({"E1001": KIM, "E2001": PARK}), inbox=inbox,
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    alert_id = alert.data["alert_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        mine = (await client.get("/api/inbox", headers={"X-User-Id": "E1001"})).json()
        assert [a["alert_id"] for a in mine] == [alert_id] and mine[0]["read_at"] is None
        assert mine[0]["counts"]["7일 이내"] == 2
        opened = await client.get(f"/api/inbox/{alert_id}", headers={"X-User-Id": "E1001"})
        assert opened.status_code == 200 and opened.json()["read_at"]
        assert (await client.get("/api/inbox", headers={"X-User-Id": "E2001"})).json() == []
        assert (await client.get(f"/api/inbox/{alert_id}", headers={"X-User-Id": "E2001"})).status_code == 404


@pytest.fixture
def mail_checker(loans):
    rules, default = load_policy(ROOT / "config/permissions.yaml")
    return CustomerMailRule(create_checker(rules=rules, default=default), loans)


def mail(to, cc=(), principal=KIM, subject="만기 안내", body="안내"):
    return call("send_mail", {"to": list(to), "cc": list(cc), "subject": subject, "body": body}, principal)


async def prepared(loans, days=7, principal=KIM):
    return (await PrepareMaturityNoticesTool(loans).run(call("prepare_maturity_notices", {"days": days}, principal))).data


async def test_prepare_notices_one_per_customer(loans):
    data = await prepared(loans, 30)
    assert data["count"] == 5 and [s["customer"] for s in data["skipped"]] == ["이*준"]  # 이메일 없는 고객
    assert all(len(n["to"]) == 1 for n in data["notices"])
    overdue, soon = data["notices"][0], data["notices"][1]
    assert overdue["to"] == ["hong.gildong@example.com"] and "만기일(2026-11-18)이 지났습니다" in overdue["body"]
    assert "2026-11-26" in soon["body"] and "5억원" in soon["body"] and "홍길동" not in overdue["body"]
    assert (await prepared(loans, 30, LEE))["notices"] == []
    # 같은 입력이면 같은 글 (본문 검사의 전제)
    assert data == await prepared(loans, 30)


async def test_customer_mail_goes_only_through_the_notice_tool(loans, mail_checker):
    """양식 그대로여도 고객 이메일로 send_mail 을 직접 쓰면 거부한다 (발송 기록이 send_maturity_notice 에만 있다)."""
    notice = (await prepared(loans))["notices"][1]
    changed = notice["body"].replace("2026-11-26", "2026-12-26")
    for bad in (mail(notice["to"], subject=notice["subject"], body=notice["body"]),
                mail(notice["to"], subject=notice["subject"], body=changed),
                mail(notice["to"], subject="다른 제목", body=notice["body"])):
        verdict = await mail_checker.check(bad)
        assert verdict.decision is Decision.DENY and verdict.rule_id == "customer-mail-notice-only"


async def test_one_customer_per_mail_and_own_customers_only(loans, mail_checker):
    notice = render_notice(loans.items_for_email(KIM, "hong.gildong@example.com")[0])
    one = await mail_checker.check(mail(["Hong.Gildong@example.com"], ["colleague@bank.example"], subject=notice["subject"], body=notice["body"]))
    assert one.decision is Decision.DENY and one.rule_id == "customer-mail-notice-only"
    mixed = await mail_checker.check(mail(["hong.gildong@example.com"], ["minji.kim@example.com"]))
    assert mixed.decision is Decision.DENY and mixed.rule_id == "customer-mail-one-per-customer"
    other = await mail_checker.check(mail(["sua.jung@example.com"]))
    assert other.decision is Decision.DENY and other.rule_id == "customer-mail-own-only"
    # 관리자도 섞어 보낼 수 없다
    assert (await mail_checker.check(mail(["hong.gildong@example.com", "sua.jung@example.com"], principal=ADMIN))).decision is Decision.DENY
    # 고객이 아닌 메일과 다른 툴은 원래 정책 그대로
    assert (await mail_checker.check(mail(["a@bank.example", "b@bank.example"]))).decision is Decision.ASK
    assert (await mail_checker.check(call("loans_maturing", {}))).decision is Decision.ALLOW
    assert (await mail_checker.check(call("send_maturity_alert", {}))).decision is Decision.ALLOW


class FakeMailer:
    def __init__(self):
        self.calls = []

    async def run(self, call):
        self.calls.append(call)
        return ToolResult(call.call_id, True, "{}", data={"status": "simulated"})


async def test_notice_card_shows_mail_and_send_uses_template(loans, mail_checker):
    card = await mail_checker.check(call("send_maturity_notice", {"loan_id": "L-1002"}))
    assert card.decision is Decision.ASK
    assert "finance@hanbit-precision.example.com" in card.reason and "2026-11-26" in card.reason and "5억원" in card.reason
    for loan_id, who in (("L-2001", KIM), ("L-1004", KIM), ("L-1002", PARK), ("L-1002", ADMIN)):
        assert (await mail_checker.check(call("send_maturity_notice", {"loan_id": loan_id}, who))).decision is Decision.DENY

    mailer = FakeMailer()
    tool = SendMaturityNoticeTool(loans, mailer)
    sent = await tool.run(call("send_maturity_notice", {"loan_id": "L-1002"}))
    assert sent.ok and sent.data["to"] == ["finance@hanbit-precision.example.com"]
    args = mailer.calls[0].arguments
    assert mailer.calls[0].tool_name == "send_mail" and args["cc"] == []
    assert args["body"] == render_notice(loans.own_item(KIM, "L-1002"))["body"]
    assert not (await tool.run(call("send_maturity_notice", {"loan_id": "L-2001"}))).ok  # 남의 고객
    assert not (await tool.run(call("send_maturity_notice", {"loan_id": "L-1002", "body": "x"}))).ok  # 본문은 못 넘긴다
    assert not (await SendMaturityNoticeTool(loans).run(call("send_maturity_notice", {"loan_id": "L-1002"}))).ok
    assert len(mailer.calls) == 1


class LostMailer(FakeMailer):
    """응답이 끊겨 보냈는지 모르는 경우 (tools/gmail.py 의 문구)."""

    async def run(self, call):
        self.calls.append(call)
        return ToolResult(call.call_id, False, "Gmail MCP 서버에 연결하거나 응답을 읽지 못했습니다. 발송 여부가 불확실하므로 보낸편지함을 확인한 후 재시도해 주세요.")


class RefusedMailer(FakeMailer):
    async def run(self, call):
        self.calls.append(call)
        return ToolResult(call.call_id, False, "본인 Gmail 계정이 연결되지 않았습니다.")


async def test_same_notice_is_sent_only_once(loans, inbox, tmp_path):
    notices = NoticeLog(tmp_path / "notices.sqlite3")
    checker = CustomerMailRule(create_checker(rules=load_policy(ROOT / "config/permissions.yaml")[0], default=Decision.ASK),
                               loans, notices)
    mailer = FakeMailer()
    tool = SendMaturityNoticeTool(loans, mailer, notices, inbox)
    sent = await tool.run(call("send_maturity_notice", {"loan_id": "L-1002"}))
    assert sent.ok and len(mailer.calls) == 1
    # 준비 단계에서 빠지고 (UC-05-1 3a), 승인 카드 전에 거부되고, 도구도 다시 보내지 않는다
    data = (await PrepareMaturityNoticesTool(loans, notices).run(call("prepare_maturity_notices", {"days": 7}))).data
    assert [n["loan_id"] for n in data["notices"]] == ["L-1001", "L-1003"]
    assert data["already"][0]["loan_id"] == "L-1002" and data["already"][0]["status"] == "이미 발송함"
    card = await checker.check(call("send_maturity_notice", {"loan_id": "L-1002"}))
    assert card.decision is Decision.DENY and card.rule_id == "maturity-notice-once" and "이미 보냈습니다" in card.reason
    again = await tool.run(call("send_maturity_notice", {"loan_id": "L-1002"}))
    assert not again.ok and "이미 보냈거나" in again.content and len(mailer.calls) == 1
    # 결과를 모르면 sending 으로 남아 다시 보내지 않는다
    lost = await SendMaturityNoticeTool(loans, LostMailer(), notices, inbox).run(call("send_maturity_notice", {"loan_id": "L-1001"}))
    assert not lost.ok and (await notices.active("L-1001", "2026-11-18"))["status"] == "sending"
    assert (await checker.check(call("send_maturity_notice", {"loan_id": "L-1001"}))).decision is Decision.DENY
    # 확실한 실패는 다시 보낼 수 있다
    refused = await SendMaturityNoticeTool(loans, RefusedMailer(), notices, inbox).run(call("send_maturity_notice", {"loan_id": "L-1003"}))
    assert not refused.ok and await notices.active("L-1003", "2026-11-30") is None
    assert (await checker.check(call("send_maturity_notice", {"loan_id": "L-1003"}))).decision is Decision.ASK
    # 결과는 직원 알림함에 남는다 (UC-05-2)
    titles = [a["title"] for a in await inbox.list("E1001")]
    assert any(t.startswith("고객 안내 발송: (주)한빛정밀") for t in titles)
    assert any("확인 필요" in t for t in titles) and any("실패" in t for t in titles)
    assert [h["status"] for h in await notices.history("E1001")] == ["failed", "sending", "sent"]


async def test_rejected_notice_is_recorded_and_can_be_requested_again(loans, inbox, tmp_path):
    notices = NoticeLog(tmp_path / "notices.sqlite3")
    checker = CustomerMailRule(create_checker(rules=load_policy(ROOT / "config/permissions.yaml")[0], default=Decision.ASK),
                               loans, notices)
    mailer = FakeMailer()
    tool = SendMaturityNoticeTool(loans, mailer, notices, inbox)
    runner = create_harness(
        llm=ScriptedLLM([{"tool_calls": [{"id": "c1", "name": "send_maturity_notice", "arguments": {"loan_id": "L-1002"}}]}]),
        tools=SimpleToolRegistry([tool]), checker=checker, approver=FixedApprover(False), audit=MemoryAudit(), max_turns=3)
    result = await runner.run("L-1002 보내줘", KIM, "s")
    assert result.stop_reason == "denied" and mailer.calls == []
    assert [h["status"] for h in await notices.history("E1001")] == ["rejected"]
    assert (await inbox.list("E1001"))[0]["title"].startswith("고객 안내 반려")
    # 반려는 보낸 것이 아니므로 다시 요청할 수 있다
    assert (await checker.check(call("send_maturity_notice", {"loan_id": "L-1002"}))).decision is Decision.ASK
