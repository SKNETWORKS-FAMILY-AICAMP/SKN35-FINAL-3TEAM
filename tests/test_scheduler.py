"""예약 실행 (services/scheduler.py, config/schedules.yaml)."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from contracts.permission import Decision, Principal
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import load_policy
from services.container import AppServices
from services.inbox import InboxStore
from services.scheduler import Scheduler, load_jobs, next_run
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.loans import LoanStore
from tools.maturity_alert import SendMaturityAlertTool
from tools.registry import SimpleToolRegistry

ROOT = Path(__file__).resolve().parents[1]
SEOUL = ZoneInfo("Asia/Seoul")
PEOPLE = {"E1001": Principal("E1001", ("member",), {"dept_id": 10, "security_grade": 2}),
          "E2001": Principal("E2001", ("member",), {"dept_id": 20, "security_grade": 2}),
          "E0001": Principal("E0001", ("admin",), {"dept_id": 1, "security_grade": 5})}


def services(tmp_path):
    inbox = InboxStore(tmp_path / "inbox.sqlite3")
    rules, default = load_policy(ROOT / "config/permissions.yaml")
    return AppServices(
        llm=ScriptedLLM([]), tools=SimpleToolRegistry([SendMaturityAlertTool(LoanStore(ROOT / "data/fixtures/loans_demo.json"), inbox)]),
        checker=create_checker(rules=rules, default=default), approvals=FutureApprovalHandler(1),
        audit=JsonlAuditLogger(tmp_path / "audit.jsonl"), users=UserDirectory(PEOPLE), inbox=inbox,
    )


def test_jobs_run_on_weekdays_only():
    tz, jobs = load_jobs(ROOT / "config/schedules.yaml")
    assert [(j.id, j.at) for j in jobs] == [("market_collect", time(9, 10)), ("market_report", time(9, 20)),
                                           ("maturity_alert", time(9, 30))]
    friday_evening = datetime(2026, 11, 27, 18, 0, tzinfo=SEOUL)
    assert next_run(friday_evening, time(9, 30)) == datetime(2026, 11, 30, 9, 30, tzinfo=SEOUL)  # 월요일
    tuesday_morning = datetime(2026, 11, 24, 8, 0, tzinfo=SEOUL)
    assert next_run(tuesday_morning, time(9, 10)) == datetime(2026, 11, 24, 9, 10, tzinfo=SEOUL)


async def test_maturity_alert_goes_to_each_officer_once(tmp_path):
    svc = services(tmp_path)
    scheduler = Scheduler(svc, ROOT / "config/schedules.yaml")
    first = await scheduler.run_job("maturity_alert")
    assert first["status"] == "ok" and first["result"]["alerts"] == {"E1001": "보냄", "E2001": "보냄"}
    assert len(await svc.inbox.list("E1001")) == 1 and len(await svc.inbox.list("E2001")) == 1
    again = await scheduler.run_job("maturity_alert", trigger="schedule")
    assert again["result"]["alerts"] == {"E1001": "이미 보냄", "E2001": "이미 보냄"}
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").count('"send_maturity_alert"') == 4  # 권한 검사·감사 기록


async def test_jobs_without_their_tools_are_skipped_not_crashed(tmp_path):
    scheduler = Scheduler(services(tmp_path), ROOT / "config/schedules.yaml")
    assert "skipped" in (await scheduler.run_job("market_collect"))["result"]
    assert "skipped" in (await scheduler.run_job("market_report"))["result"]


async def test_only_admin_sees_and_runs_jobs(tmp_path):
    svc = services(tmp_path)
    app = create_app(build=lambda: svc)
    app.state.services = svc
    app.state.scheduler = Scheduler(svc, ROOT / "config/schedules.yaml")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/admin/schedules", headers={"X-User-Id": "E1001"})).status_code == 403
        listing = (await client.get("/api/admin/schedules", headers={"X-User-Id": "E0001"})).json()
        assert [j["id"] for j in listing["jobs"]] == ["market_collect", "market_report", "maturity_alert"]
        ran = await client.post("/api/admin/schedules/maturity_alert/run", headers={"X-User-Id": "E0001"})
        assert ran.status_code == 200 and ran.json()["trigger"] == "manual"
        assert (await client.post("/api/admin/schedules/nope/run", headers={"X-User-Id": "E0001"})).status_code == 404


async def test_market_jobs_go_through_the_market_mcp(tmp_path):
    """09:10 수집은 MCP 의 collect_market_data 로, 09:20 리포트는 시장 툴만 준 하네스로 (코드가 순서를 정함)."""
    from datetime import date

    from mcp import Client

    from market_mcp.collect import DbProvider, MemoryMarketStore, collect_and_load
    from market_mcp.ecos import ReplayProvider
    from market_mcp.server import build_server
    from market_mcp.service import MarketService
    from services.documents import DocumentStore
    from tools.market import LOCAL_TOOLS, LocalReportTool, MarketMCPConnection, MarketTool, report_service

    sample = ROOT / "mcp-servers/market/src/market_mcp/resources/ecos_delivery_sample.json"
    store = MemoryMarketStore()

    async def collector(days):
        return await collect_and_load(ReplayProvider(sample), store, tmp_path / "inbox", days=days, today=date(2026, 11, 18))

    server = build_server(MarketService(DbProvider(store), today=lambda: date(2026, 11, 18)), collector)
    connection = MarketMCPConnection("http://127.0.0.1:8002/mcp", client_factory=lambda: Client(server))
    documents = DocumentStore(tmp_path / "docs.sqlite3")
    reports = report_service(documents, ROOT / "config/report_templates")
    svc = services(tmp_path)
    svc.tools = SimpleToolRegistry([MarketTool("market_snapshot", connection, reports.store),
                                    *(LocalReportTool(name, reports) for name in LOCAL_TOOLS)])
    svc.llm = ScriptedLLM([{"content": "USD는 1,338.80원입니다."}, {"content": "USD는 1,338.80원입니다."}])
    scheduler = Scheduler(svc, ROOT / "config/schedules.yaml")
    collected = await scheduler.run_job("market_collect")
    assert collected["status"] == "ok" and collected["result"]["status"] == "loaded"
    assert collected["result"]["detail"]["USD"]["accepted"] > 0
    reported = await scheduler.run_job("market_report")
    assert reported["result"]["reports"] == {"E1001": "저장", "E2001": "저장"}
    assert [d["kind"] for d in await documents.list("E1001")] == ["market_report"]
