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
from services.inbox import InboxStore
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.loans import LoansMaturingTool, LoanStore, mask_name, won
from tools.maturity_alert import SendMaturityAlertTool
from contracts.harness import ToolResult
from tools.maturity_notice import PrepareMaturityNoticesTool, SendMaturityNoticeTool, render_notice
from tools.number_check import unknown_numbers
from tools.registry import SimpleToolRegistry

ROOT = Path(__file__).resolve().parents[1]
KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
PARK = Principal("park", ("member",), {"dept_id": 20, "security_grade": 2})
LEE = Principal("lee", ("dept_head",), {"dept_id": 10, "security_grade": 3})
ADMIN = Principal("admin", ("admin",), {"dept_id": 1, "security_grade": 5})


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
    for arguments in ({"officer_id": "park"}, {"days": 0}, {"days": 91}, {"days": "30"}):
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
    bad = await tool.run(call("send_maturity_alert", {"days": 30, "summary": "총 47건 중 9건이 급합니다."}))
    assert bad.ok and bad.data["summary_dropped"] and "47, 9" in bad.data["message"]
    stored = inbox.open("kim", bad.data["alert_id"])
    assert stored["summary"] == "" and len(stored["payload"]["items"]) == 6
    # 모델이 목록을 넘겨도 받지 않는다
    assert not (await tool.run(call("send_maturity_alert", {"days": 30, "items": []}))).ok
    no_loans = await tool.run(call("send_maturity_alert", {"days": 30}, LEE))
    assert no_loans.ok and not no_loans.data["sent"] and inbox.list("lee") == []


async def test_inbox_api_only_shows_own_alerts(loans, inbox, tmp_path):
    alert = await SendMaturityAlertTool(loans, inbox).run(call("send_maturity_alert", {"days": 30}))
    svc = AppServices(
        llm=ScriptedLLM([]), tools=SimpleToolRegistry([]), checker=create_checker(rules=[], default=Decision.ASK),
        approvals=FutureApprovalHandler(1), audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory({"kim": KIM, "park": PARK}), inbox=inbox,
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    alert_id = alert.data["alert_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        mine = (await client.get("/api/inbox", headers={"X-User-Id": "kim"})).json()
        assert [a["alert_id"] for a in mine] == [alert_id] and mine[0]["read_at"] is None
        assert mine[0]["counts"]["7일 이내"] == 2
        opened = await client.get(f"/api/inbox/{alert_id}", headers={"X-User-Id": "kim"})
        assert opened.status_code == 200 and opened.json()["read_at"]
        assert (await client.get("/api/inbox", headers={"X-User-Id": "park"})).json() == []
        assert (await client.get(f"/api/inbox/{alert_id}", headers={"X-User-Id": "park"})).status_code == 404


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


async def test_customer_mail_must_be_the_prepared_notice(loans, mail_checker):
    notice = (await prepared(loans))["notices"][1]
    ok = await mail_checker.check(mail(notice["to"], subject=notice["subject"], body=notice["body"] + "\n"))
    assert ok.decision is Decision.ASK  # 줄 끝 공백 차이는 같은 글로 본다
    changed = notice["body"].replace("2026-11-26", "2026-12-26")
    for bad in (mail(notice["to"], subject=notice["subject"], body=changed),
                mail(notice["to"], subject=notice["subject"], body=notice["body"] + "\n연장은 승인될 예정입니다."),
                mail(notice["to"], subject="다른 제목", body=notice["body"])):
        verdict = await mail_checker.check(bad)
        assert verdict.decision is Decision.DENY and verdict.rule_id == "customer-mail-notice-only"


async def test_one_customer_per_mail_and_own_customers_only(loans, mail_checker):
    notice = render_notice(loans.items_for_email(KIM, "hong.gildong@example.com")[0])
    one = await mail_checker.check(mail(["Hong.Gildong@example.com"], ["colleague@bank.example"], subject=notice["subject"], body=notice["body"]))
    assert one.decision is Decision.ASK  # 원래 정책(승인 카드)으로 넘어간다
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
