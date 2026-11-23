"""UC-03 만기 알림."""

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
from services.container import AppServices
from services.inbox import InboxStore
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.loans import LoansMaturingTool, LoanStore, mask_name, won
from tools.maturity_alert import SendMaturityAlertTool
from tools.number_check import unknown_numbers
from tools.registry import SimpleToolRegistry

ROOT = Path(__file__).resolve().parents[1]
KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
PARK = Principal("park", ("member",), {"dept_id": 20, "security_grade": 2})
LEE = Principal("lee", ("dept_head",), {"dept_id": 10, "security_grade": 3})


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
