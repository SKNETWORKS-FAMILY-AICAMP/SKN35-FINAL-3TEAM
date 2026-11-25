"""실제 ECOS 인증키/외부 통신 없이 시장 MCP와 플랫폼을 함께 검증한다."""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError

import httpx
import pytest
from mcp import Client

from contracts.permission import Decision, Principal, ToolCall
from main import create_app
from market_mcp.ecos import EcosProvider, MarketError, ReplayProvider, SERIES
from market_mcp.server import build_http_app, build_server
from market_mcp.service import MarketService, check_summary, clean_rows
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import load_policy
from services.container import AppServices, build_from_env
from services.templates import InMemoryTemplateStore, load_templates
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.market import LOCAL_TOOLS, LocalReportTool, MarketMCPConnection, MarketTool, build_market_tools
from tools.registry import SimpleToolRegistry

SAMPLE = Path(__file__).resolve().parents[1] / "mcp-servers/market/src/market_mcp/resources/ecos_delivery_sample.json"
KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
PARK = Principal("park", ("member",), {"dept_id": 20, "security_grade": 2})


@pytest.fixture
def service(tmp_path):
    return MarketService(ReplayProvider(SAMPLE), tmp_path, today=lambda: date(2026, 11, 23))


@pytest.fixture
def bridge(service):
    # 실제 배치처럼 MCP 서버는 저장소 없이 조회만 한다
    server = build_server(MarketService(service.provider, today=service.today))
    return MarketMCPConnection("http://127.0.0.1:8002/mcp", client_factory=lambda: Client(server))


def market_tools(service, bridge, names=("market_snapshot", *LOCAL_TOOLS)):
    """플랫폼 배치: 조회는 MCP(결과를 플랫폼 저장소에 보관), 양식·저장·열람은 플랫폼 안에서."""
    tools = {"market_snapshot": MarketTool("market_snapshot", bridge, service.store),
             **{name: LocalReportTool(name, service) for name in LOCAL_TOOLS}}
    return [tools[name] for name in names]


def call(name, args, user=KIM):
    return ToolCall("market-call", name, args, user, "s1")


async def test_replay_snapshot_units_calculations_and_stale_dates(service):
    result = await service.market_snapshot("kim", "2026-11-18")
    usd, jpy = result["fx"][:2]
    assert (usd["value"], usd["change"], usd["change_pct"]) == (1338.8, 3.8, .28)
    assert jpy["value"] == 839.45 and jpy["unit"] == "원/100엔"
    assert result["rates"][0]["observed_since"] == "2026-08-27"
    assert result["rates"][0]["actual_date"] == "2026-11-16"
    assert result["mode"] == "replay" and "실시간 조회가 아닙니다" in result["markdown"]
    saturday = await service.market_snapshot("kim", "2026-11-21")
    assert saturday["fx"][0]["actual_date"] == "2026-11-18" and saturday["fx"][0]["stale"]


@pytest.mark.parametrize("bad", ["2026-02-30", "20261118", "not-a-date", "2026-11-24"])
async def test_invalid_or_future_dates_fail(service, bad):
    with pytest.raises(MarketError):
        await service.market_snapshot("kim", bad)


def test_invalid_rows_are_ignored_and_extreme_values_held():
    start, end = date(2026, 11, 1), date(2026, 11, 23)
    valid, held = clean_rows([["20261117", "100"], ["20261118", "110"], ["20261119", "NaN"],
                              ["bad", "1"], ["20261120", "0"], ["20261121", "102"]], start, end, "fx")
    assert [v for _, v in valid] == [100, 102] and held[0]["value"] == 110
    with pytest.raises(MarketError):
        clean_rows([["20261117", "100"], ["20261117", "101"]], start, end, "fx")


async def test_report_pins_snapshot_and_rejects_unknown_numbers_and_advice(service):
    snapshot = await service.market_snapshot("kim", "2026-11-18")
    report = await service.save_morning_report("kim", snapshot["snapshot_id"], "USD는 1,338.80원입니다. 기준금리는 3.00%입니다.")
    assert report["check"]["passed"] and "1,338.80" in report["text"]
    bad = await service.save_morning_report("kim", snapshot["snapshot_id"], "USD는 9,999원입니다. 매수를 추천합니다.")
    assert not bad["check"]["passed"] and "9,999" not in bad["text"] and "추천합니다" not in bad["text"]
    assert "1,338.80" in bad["text"] and "나머지 항목과 출처만" in bad["text"]
    loaded = await service.get_morning_report("kim", report["report_id"])
    assert loaded["text"] == report["text"]
    units = await service.save_morning_report("kim", snapshot["snapshot_id"], "USD는 1달러당 1,338.80원이며 JPY는 100엔당 839.45원입니다.")
    assert units["check"]["passed"]
    sign_error = await service.save_morning_report("kim", snapshot["snapshot_id"], "USD는 -3.80원 변동했습니다.")
    assert not sign_error["check"]["passed"]
    stale_claim = await service.save_morning_report("kim", snapshot["snapshot_id"], "기준금리는 2026년 11월 16일 이후 변동이 없었습니다.")
    assert not stale_claim["check"]["passed"]
    currency_count = await service.save_morning_report("kim", snapshot["snapshot_id"], "4개 통화의 환율을 확인했습니다.")
    assert currency_count["check"]["passed"]
    wrong_count = await service.save_morning_report("kim", snapshot["snapshot_id"], "2개 통화의 환율을 확인했습니다.")
    assert not wrong_count["check"]["passed"]


async def test_other_users_cannot_save_or_read_snapshot_and_reports(service):
    snapshot = await service.market_snapshot("kim", "2026-11-18")
    with pytest.raises(MarketError, match="찾을 수 없습니다"):
        await service.save_morning_report("park", snapshot["snapshot_id"], "")
    report = await service.save_morning_report("kim", snapshot["snapshot_id"], "")
    for ident in (report["report_id"], "../reports", "0" * 32):
        with pytest.raises(MarketError, match="찾을 수 없습니다"):
            await service.get_morning_report("park", ident)


async def test_live_provider_builds_fixed_url_and_sanitizes_key_errors():
    seen = []
    def fetch(url):
        seen.append(url)
        return {"StatisticSearch": {"list_total_count": 1, "row": [{"TIME": "20261118", "DATA_VALUE": "1338.8"}]}}
    provider = EcosProvider("private-key-never-leak", fetch_json=fetch)
    rows = await provider.fetch(SERIES[0], date(2026, 11, 1), date(2026, 11, 18))
    assert rows == [["20261118", "1338.8"]]
    assert seen[0].startswith("https://ecos.bok.or.kr/api/StatisticSearch/") and "/731Y001/D/" in seen[0]
    def failing(url):
        raise HTTPError(url, 403, "denied", {}, None)
    provider.fetch_json = failing
    with pytest.raises(MarketError) as exc:
        await provider.fetch(SERIES[0], date(2026, 11, 1), date(2026, 11, 18))
    assert "private-key-never-leak" not in str(exc.value) and "https" not in str(exc.value)
    with pytest.raises(MarketError, match="ECOS_API_KEY"):
        await EcosProvider().fetch(SERIES[0], date(2026, 11, 1), date(2026, 11, 18))


async def test_bridge_three_tools_and_identity_cannot_be_spoofed(service, bridge):
    tools = {tool.spec.name: tool for tool in market_tools(service, bridge)}
    snapshot = await tools["market_snapshot"].run(call("market_snapshot", {"base_date": "2026-11-18"}))
    assert snapshot.ok and snapshot.data["mode"] == "replay"
    saved = await tools["save_morning_report"].run(call("save_morning_report", {
        "snapshot_id": snapshot.data["snapshot_id"], "summary": "USD는 1,338.80원입니다.",
    }))
    assert saved.ok and saved.data["document_id"] == saved.data["report_id"]  # 다른 서류와 같은 내려받기 버튼
    read = await tools["get_morning_report"].run(call("get_morning_report", {"report_id": saved.data["report_id"]}))
    assert read.ok and read.data["text"] == saved.data["text"]
    denied = await tools["get_morning_report"].run(call("get_morning_report", {"report_id": saved.data["report_id"]}, PARK))
    assert not denied.ok
    # 다른 사용자는 남의 조회 결과로 저장할 수 없다
    assert not (await tools["save_morning_report"].run(call("save_morning_report", {"snapshot_id": snapshot.data["snapshot_id"]}, PARK))).ok
    for extra in ({"requester_id": "park"}, {"url": "https://example.invalid"}, {"api_key": "bad"}):
        assert not (await tools["market_snapshot"].run(call("market_snapshot", extra))).ok


async def test_mcp_only_looks_up_and_stores_nothing(service, tmp_path):
    server = build_server(MarketService(service.provider, today=service.today))
    async with Client(server) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == {"market_snapshot"}
    stateless = MarketService(service.provider, today=service.today)
    result = await stateless.market_snapshot("kim", "2026-11-18")
    assert result["snapshot_id"] and stateless.store is None
    with pytest.raises(MarketError):
        await stateless.save_morning_report("kim", result["snapshot_id"], "")


async def test_reports_are_saved_in_platform_store_and_downloaded_like_other_documents(bridge, tmp_path):
    from services.documents import DocumentStore
    from tools.market import report_service

    store = DocumentStore(tmp_path / "docs.sqlite3")
    reports = report_service(store, Path("config/report_templates"))
    tools = {tool.spec.name: tool for tool in market_tools(reports, bridge)}
    snapshot = await tools["market_snapshot"].run(call("market_snapshot", {"base_date": "2026-11-18"}))
    assert (await store.get_record("snapshots", snapshot.data["snapshot_id"], "kim"))["fx"][0]["value"] == 1338.8
    saved = await tools["save_morning_report"].run(call("save_morning_report", {"snapshot_id": snapshot.data["snapshot_id"]}))
    record = await store.get("kim", saved.data["document_id"])
    assert record["kind"] == "market_report" and record["document"]["blocks"]
    assert (await store.list("kim"))[0]["title"] == record["document"]["title"] and await store.get("park", saved.data["document_id"]) is None
    svc = AppServices(llm=ScriptedLLM([]), tools=SimpleToolRegistry([]), checker=create_checker(rules=[], default=Decision.ASK),
                      approvals=FutureApprovalHandler(1), audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
                      users=UserDirectory({"kim": KIM, "park": PARK}), documents=store)
    app = create_app(build=lambda: svc); app.state.services = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        docx = await client.get(f"/api/documents/{saved.data['document_id']}/download?format=docx", headers={"X-User-Id": "kim"})
        assert docx.status_code == 200 and docx.content[:2] == b"PK"
        other = await client.get(f"/api/documents/{saved.data['document_id']}/download?format=docx", headers={"X-User-Id": "park"})
        assert other.status_code == 404


async def test_chat_runs_mcp_lookup_and_save_then_pins_server_report_text(service, bridge, tmp_path):
    rules, default = load_policy("config/permissions.yaml")
    svc = AppServices(llm=ScriptedLLM([{"content": "USD는 1,338.80원입니다. 기준금리는 3.00%입니다."}]),
                      tools=SimpleToolRegistry(market_tools(service, bridge)),
                      checker=create_checker(rules=rules, default=default), approvals=FutureApprovalHandler(1),
                      audit=JsonlAuditLogger(tmp_path / "audit.jsonl"), users=UserDirectory({"kim": KIM, "park": PARK}))
    svc.templates = InMemoryTemplateStore(load_templates("config/agent_templates.yaml"))
    app = create_app(build=lambda: svc); app.state.services = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        h = {"X-User-Id": "kim"}
        items = (await client.get("/api/templates", headers=h)).json()
        assert next(t for t in items if t["template_id"] == "uc02-market-report")["available"]
        response = await client.post("/api/chat/messages", headers=h, json={"content": "2026-11-18 금리·환율 리포트 만들어줘"})
        body = response.json()
        assert response.status_code == 200
        assert [e["tool_name"] for e in body["tool_events"]] == [
            "search_report_templates", "get_report_template", "market_snapshot", "save_morning_report"]
        report = json.loads(body["tool_events"][-1]["result"])
        assert body["message"] == report["text"] and "1,338.80" in body["message"]
        assert len(svc.llm.calls) == 1
        read = (await client.post("/api/chat/messages", headers=h, json={"content": report["report_id"] + " 리포트 다시 열어줘"})).json()
        assert read["message"] == body["message"]


def test_registration_and_disable(monkeypatch):
    assert [t.spec.name for t in build_market_tools()] == ["market_snapshot"]
    tools = build_market_tools(MarketService(None))
    assert {t.spec.name for t in tools} == {
        "search_report_templates", "get_report_template", "market_snapshot", "save_morning_report", "get_morning_report"}
    assert all("requester_id" not in t.spec.parameters["properties"] for t in tools)
    monkeypatch.setenv("MARKET_MCP_URL", "")
    assert build_market_tools() == []


def test_market_explanations_do_not_implicitly_create_reports():
    from harness.market_grounding import market_plan
    specs = [tool.spec for tool in build_market_tools(MarketService(None))]
    assert market_plan("금리·환율 리포트란 무슨 의미야?", specs) is None
    assert market_plan("금리·환율 조회", specs)[2] is False
    assert market_plan("금리·환율 리포트 만들어줘", specs)[2] is True


def test_mcp_transport_requires_configured_token(service):
    from fastapi.testclient import TestClient
    with TestClient(build_http_app(service, token="test-market-token")) as client:
        assert client.get("/health").json()["mode"] == "replay"
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401


async def test_partial_and_complete_api_outages_are_distinguished(tmp_path):
    class Partial:
        mode = "live"
        async def fetch(self, series, start, end):
            if series.code == "USD":
                return [["20261118", "1338.8"]]
            raise OSError("private-url-must-not-be-returned")
    svc = MarketService(Partial(), tmp_path, today=lambda: date(2026, 11, 23))
    result = await svc.market_snapshot("kim", "2026-11-18")
    assert not result["fx"][0].get("missing") and result["fx"][1]["missing"]
    assert "private-url" not in json.dumps(result)
    class Down(Partial):
        async def fetch(self, series, start, end):
            raise OSError("down")
    with pytest.raises(MarketError, match="조회하지 못했습니다"):
        await MarketService(Down(), tmp_path).market_snapshot("kim", "2026-11-18")


async def test_save_denial_does_not_create_report(bridge, service, tmp_path):
    from tests.fakes import FixedApprover, MemoryAudit, ScriptedChecker
    from harness import create_harness
    runner = create_harness(llm=ScriptedLLM([]),
        tools=SimpleToolRegistry(market_tools(service, bridge, ("market_snapshot", "save_morning_report"))),
        checker=ScriptedChecker({"save_morning_report": Decision.DENY}), approver=FixedApprover(True),
        audit=MemoryAudit(), max_turns=3)
    result = await runner.run("2026-11-18 금리·환율 리포트 표만 저장", KIM, "s")
    assert result.stop_reason == "denied" and not (service.root / "reports").exists()
    assert "등록되었습니다" not in result.final_message.content


async def test_snapshot_only_does_not_call_llm(bridge, tmp_path):
    from tests.fakes import FixedApprover, MemoryAudit, ScriptedChecker
    from harness import create_harness
    model = ScriptedLLM([])
    runner = create_harness(llm=model, tools=SimpleToolRegistry([MarketTool("market_snapshot", bridge)]),
        checker=ScriptedChecker({}), approver=FixedApprover(True), audit=MemoryAudit(), max_turns=3)
    result = await runner.run("2026-11-18 환율 조회", KIM, "s")
    assert "1,338.80" in result.final_message.content and not model.calls


async def test_connection_error_is_reported_without_retry_or_record(service):
    calls = []
    def down():
        calls.append(1)
        raise OSError("down")
    connection = MarketMCPConnection("http://127.0.0.1:8002/mcp", client_factory=down)
    result = await MarketTool("market_snapshot", connection, service.store).run(call("market_snapshot", {"base_date": "2026-11-18"}))
    assert not result.ok and len(calls) == 1 and "서버 실행 상태" in result.content
    assert not (service.root / "snapshots").exists()


def test_replay_file_hash_is_checked(tmp_path):
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    payload["series"][0]["rows"][0][1] = "9999"
    changed = tmp_path / "tampered.json"
    changed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MarketError, match="해시"):
        ReplayProvider(changed)
