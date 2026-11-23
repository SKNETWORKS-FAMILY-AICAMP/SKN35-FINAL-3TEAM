"""양식 검색·원본 전체 읽기·교체·버전 고정과 MCP 작성 흐름."""

import json
import shutil
from pathlib import Path

import pytest

from harness import create_harness
from market_mcp.ecos import MarketError
from market_mcp.report_templates import ReportTemplateCatalog, render_template, validate_template
from tests.fakes import FixedApprover, MemoryAudit, ScriptedChecker, ScriptedLLM
from tests.test_market_tools import KIM, bridge, market_tools, service  # noqa: F401
from tools.registry import SimpleToolRegistry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def templates(tmp_path, service):  # noqa: F811
    directory = tmp_path / "templates"
    shutil.copytree(ROOT / "config/report_templates", directory)
    service.templates = ReportTemplateCatalog(directory)
    return directory


@pytest.mark.parametrize("query,expected", [
    ("일일 금리·환율 보고서로 작성", "market-daily"),
    ("간단한 환율 브리핑으로 작성", "fx-brief"),
    ("금리·환율 자료 누락 점검 보고서", "market-check"),
])
async def test_matching_template_is_selected(service, query, expected):  # noqa: F811
    result = await service.search_report_templates("kim", query)
    assert result["search_backend"] == "local_keywords"
    assert result["matches"][0]["template_id"] == expected
    assert "body_markdown" not in result["matches"][0]  # 검색 후보와 원본 읽기를 분리한다.


async def test_full_template_is_read_and_saved_with_the_report(service):  # noqa: F811
    results = await service.search_report_templates("kim", "간단 환율 브리핑")
    candidate = results["matches"][0]
    selected = await service.get_report_template("kim", candidate["template_id"], candidate["template_hash"])
    template = selected["template"]
    assert any(s.get("binding") == "fx_table" for s in template["sections"]) and "source_notes" in template["required_bindings"]
    snapshot = await service.market_snapshot("kim", "2026-11-18")
    report = await service.save_morning_report("kim", snapshot["snapshot_id"], "USD는 1,338.80원입니다.", selected["template_selection_id"])
    assert report["template"]["id"] == "fx-brief"
    assert "환율 간단 브리핑" in report["text"] and "주요 환율" in report["text"]
    assert "### 기준금리" not in report["text"] and "## 3. 한국은행 기준금리" not in report["text"]
    assert "테스트용 합성 양식" in report["text"] and "실시간 조회가 아닙니다" in report["text"]
    assert "{{" not in report["text"]


async def test_file_edits_apply_to_next_report_but_not_old_report(service, templates):  # noqa: F811
    first = await service.get_report_template("kim", "market-daily")
    snapshot = await service.market_snapshot("kim", "2026-11-18")
    path = templates / "market-daily.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["version"] = 3
    changed["name"] = "회사 테스트 일일 보고서"
    next(s for s in changed["sections"] if s.get("text") == "1. 핵심 요약")["text"] = "1. 회사용 검토 메모"
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    # 이미 읽어 둔 선택은 v1 그대로, 새 읽기와 새 보고서는 v2가 된다.
    old = await service.save_morning_report("kim", snapshot["snapshot_id"], "", first["template_selection_id"])
    latest = await service.get_report_template("kim", "market-daily")
    new = await service.save_morning_report("kim", snapshot["snapshot_id"], "", latest["template_selection_id"])
    assert old["template"]["version"] == 2 and "회사용 검토 메모" not in old["text"]
    assert new["template"]["version"] == 3 and "회사용 검토 메모" in new["text"]
    assert new["template"]["hash"] != old["template"]["hash"]
    assert (await service.get_morning_report("kim", old["report_id"]))["text"] == old["text"]


async def test_changed_search_result_is_not_silently_replaced(service, templates):  # noqa: F811
    candidate = (await service.search_report_templates("kim", "일일 금리·환율"))["matches"][0]
    path = templates / "market-daily.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["sections"].append({"type": "paragraph", "text": "회사에서 추가한 항목"})
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MarketError, match="변경"):
        await service.get_report_template("kim", candidate["template_id"], candidate["template_hash"])


async def test_other_user_cannot_use_template_selection(service):  # noqa: F811
    selected = await service.get_report_template("kim", "fx-brief")
    snapshot = await service.market_snapshot("park", "2026-11-18")
    with pytest.raises(MarketError, match="찾을 수 없습니다"):
        await service.save_morning_report("park", snapshot["snapshot_id"], "", selected["template_selection_id"])


async def test_disabled_template_cannot_create_new_report_even_if_already_read(service, templates):  # noqa: F811
    selected = await service.get_report_template("kim", "fx-brief")
    snapshot = await service.market_snapshot("kim", "2026-11-18")
    old = await service.save_morning_report("kim", snapshot["snapshot_id"], "", selected["template_selection_id"])
    path = templates / "fx-brief.json"
    raw = json.loads(path.read_text(encoding="utf-8")); raw["enabled"] = False
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MarketError, match="찾을 수 없습니다"):
        await service.save_morning_report("kim", snapshot["snapshot_id"], "", selected["template_selection_id"])
    assert (await service.get_morning_report("kim", old["report_id"]))["text"] == old["text"]


@pytest.mark.parametrize("change", [
    {"body_markdown": "{{base_date}} {{source_notes}} {{api_key}}"},
    {"body_markdown": "출처 없는 보고서 {{base_date}}"},
    {"required_bindings": ["unsupported_field"]},
    {"version": 0},
])
def test_invalid_template_cannot_be_rendered(change):
    raw = json.loads((ROOT / "config/report_templates/market-daily.json").read_text(encoding="utf-8"))
    with pytest.raises(MarketError):
        validate_template({**raw, **change})


async def test_required_summary_is_not_silently_replaced(service, templates):  # noqa: F811
    path = templates / "market-daily.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["required_bindings"].append("summary")
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    snapshot = await service.market_snapshot("kim", "2026-11-18")
    with pytest.raises(MarketError, match="필수 자료"):
        await service.save_morning_report("kim", snapshot["snapshot_id"], "")
    assert not (service.root / "reports").exists()


def runner_for(service, connection, script=()):  # noqa: F811
    return create_harness(llm=ScriptedLLM(list(script)), tools=SimpleToolRegistry(market_tools(service, connection)),
        checker=ScriptedChecker({}), approver=FixedApprover(True), audit=MemoryAudit(), max_turns=3)


@pytest.mark.parametrize("question,title", [
    ("2026-11-18 일일 금리·환율 보고서로 표만 작성해줘", "일일 금리·환율 보고서"),
    ("2026-11-18 간단 환율 브리핑 양식으로 표만 작성해줘", "환율 간단 브리핑"),
    ("2026-11-18 금리·환율 점검 보고서로 표만 작성해줘", "금리·환율 자료 점검 보고서"),
])
async def test_agent_uses_search_read_data_save_order(service, bridge, question, title):  # noqa: F811
    result = await runner_for(service, bridge).run(question, KIM, "s")
    names = [c.tool_name for message in result.messages for c in message.tool_calls]
    assert names == ["search_report_templates", "get_report_template", "market_snapshot", "save_morning_report"]
    assert title in result.final_message.content
    assert "1,338.80" in result.final_message.content


async def test_missing_template_does_not_fabricate_report(bridge, service):  # noqa: F811
    result = await runner_for(service, bridge).run("회의록 양식으로 작성해줘", KIM, "s")
    assert "양식을 찾지 못했습니다" in result.final_message.content
    assert not (service.root / "reports").exists() and not (service.root / "snapshots").exists()


async def test_ambiguous_template_asks_before_fetching_data(bridge, service, templates):  # noqa: F811
    path = templates / "market-daily.json"
    changed = json.loads(path.read_text(encoding="utf-8")); changed["priority"] = 0
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    result = await runner_for(service, bridge).run("금리·환율 리포트 만들어줘", KIM, "s")
    assert "어느 양식을 사용할지" in result.final_message.content
    assert not (service.root / "snapshots").exists()


async def test_template_listing_does_not_fetch_market_data(bridge, service):  # noqa: F811
    result = await runner_for(service, bridge).run("보고서 양식 목록 보여줘", KIM, "s")
    assert "일일 금리·환율 보고서" in result.final_message.content and "간단 환율 브리핑" in result.final_message.content
    assert not (service.root / "snapshots").exists()


async def test_long_template_is_kept_in_full_without_overloading_summary_context(bridge, service, templates):  # noqa: F811
    path = templates / "market-daily.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    padding = "회사 고정 안내 문구입니다. " * 500
    raw["sections"].append({"type": "paragraph", "text": padding})
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    runner = runner_for(service, bridge, [{"content": "USD는 1,338.80원입니다."}])
    result = await runner.run("2026-11-18 일일 금리·환율 보고서 작성해줘", KIM, "s")
    assert padding in result.final_message.content
    model_input = runner.llm.calls[0][0][-1].content
    assert len(model_input) < 5000 and padding[:300] not in model_input


def test_placeholder_values_are_not_executed_or_recursively_substituted():
    raw = json.loads((ROOT / "config/report_templates/fx-brief.json").read_text(encoding="utf-8"))
    raw.pop("sections")
    raw["body_markdown"] = "{{base_date}} {{summary}} {{fx_table}} {{source_notes}} {{report_id}}"
    template = validate_template(raw)
    bindings = {key: "값" for key in template["bindings"]}
    bindings["summary"] = "{{prepared_by}}"
    result = render_template(template, bindings, set(bindings))
    assert "{{prepared_by}}" in result


async def test_new_report_stores_structured_json_without_markdown_and_roundtrips(service):
    snapshot = await service.market_snapshot("kim", "2026-11-18")
    saved = await service.save_morning_report("kim", snapshot["snapshot_id"], "USD는 1,338.80원입니다.")
    stored = json.loads((service.root / "reports" / (saved["report_id"] + ".json")).read_text(encoding="utf-8"))
    assert "document" in stored and "markdown" not in stored and "markdown" not in saved
    assert "markdown" not in stored["snapshot"] and "body_markdown" not in stored["template"]
    document = stored["document"]
    assert document["schema_version"] == 1
    table = next(block for block in document["blocks"] if block["type"] == "table")
    assert table["columns"][0] == "통화" and table["rows"][0][1] == "1,338.80"
    assert stored["snapshot"]["fx"][0]["value"] == 1338.8
    loaded = await service.get_morning_report("kim", saved["report_id"])
    assert loaded["document"] == document == saved["document"]


async def test_legacy_saved_report_is_not_rewritten(service):
    ident = "b" * 32
    legacy = {"id": ident, "owner_user_id": "kim", "base_date": "2026-11-18", "mode": "replay",
              "check": {"passed": True}, "markdown": "# 이전 보고서\n\n보존할 원문"}
    service._write("reports", legacy)
    loaded = await service.get_morning_report("kim", ident)
    assert loaded["markdown"] == legacy["markdown"] and "document" not in loaded
    assert service._read("reports", ident, "kim") == legacy
