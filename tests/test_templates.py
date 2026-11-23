"""에이전트 템플릿: 누구나 보고, 관리자만 만들고 고친다. 툴이 없는 템플릿은 '준비 중'."""

from __future__ import annotations

from pathlib import Path

from services.templates import InMemoryTemplateStore, load_templates
from tests.test_agents import ADMIN, KIM, h, make

ROOT = Path(__file__).resolve().parents[1]
BODY = {"name": "계산", "system_prompt": "계산은 툴로 한다.", "tools": ["add"], "example": "1+2"}


def test_seed_templates_load_and_cover_use_cases():
    seeds = {t.template_id: t for t in load_templates(ROOT / "config/agent_templates.yaml")}
    assert {"uc01-regulation", "uc02-market-report", "uc03-maturity-alert", "uc05-maturity-notice"} <= set(seeds)
    assert all(t.system_prompt and t.example for t in seeds.values())
    assert load_templates(ROOT / "nope.yaml") == []


async def test_everyone_lists_and_missing_tools_mark_unavailable(tmp_path):
    client, svc = make(tmp_path=tmp_path)
    svc.templates = InMemoryTemplateStore(load_templates(ROOT / "config/agent_templates.yaml"))
    items = {t["template_id"]: t for t in (await client.get("/api/templates", headers=h(KIM))).json()}
    report = items["uc02-market-report"]
    assert report["available"] is False and report["missing_tools"] == ["market_snapshot", "save_morning_report"]


async def test_only_admin_manages_templates(tmp_path):
    client, _ = make(tmp_path=tmp_path)
    assert (await client.post("/api/templates", json=BODY, headers=h(KIM))).status_code == 403

    r = await client.post("/api/templates", json=BODY, headers=h(ADMIN))
    assert r.status_code == 201 and r.json()["available"] is True and r.json()["created_by"] == "admin"
    tid = r.json()["template_id"]
    url = f"/api/templates/{tid}"

    assert (await client.patch(url, json={"name": "x"}, headers=h(KIM))).status_code == 403
    r = await client.patch(url, json={"name": "계산 도우미", "tools": ["add", "multiply"]}, headers=h(ADMIN))
    assert (r.json()["name"], r.json()["tools"]) == ("계산 도우미", ["add", "multiply"])
    bad = await client.patch(url, json={"tools": ["send_rocket"]}, headers=h(ADMIN))
    assert bad.status_code == 422  # 화면에서는 등록된 툴로만 만든다

    assert (await client.delete(url, headers=h(KIM))).status_code == 403
    assert (await client.delete(url, headers=h(ADMIN))).status_code == 204
    assert (await client.get("/api/templates", headers=h(KIM))).json() == []
    assert (await client.delete(url, headers=h(ADMIN))).status_code == 404


async def test_admin_can_edit_template_keeping_pending_tools(tmp_path):
    client, svc = make(tmp_path=tmp_path)
    svc.templates = InMemoryTemplateStore(load_templates(ROOT / "config/agent_templates.yaml"))
    url = "/api/templates/uc02-market-report"
    r = await client.patch(url, json={"name": "아침 리포트", "tools": ["market_snapshot", "save_morning_report", "add"]}, headers=h(ADMIN))
    assert r.status_code == 200 and r.json()["missing_tools"] == ["market_snapshot", "save_morning_report"]
    assert (await client.patch(url, json={"tools": ["send_rocket"]}, headers=h(ADMIN))).status_code == 422
