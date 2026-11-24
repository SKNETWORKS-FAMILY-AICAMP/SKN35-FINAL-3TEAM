"""UC-02-1 시장 데이터 수집기·적재기 (mcp-servers/market/src/market_mcp/collect.py)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from market_mcp.collect import DbProvider, MemoryMarketStore, collect_and_load, load_file, series_hash, write_delivery
from market_mcp.ecos import ReplayProvider
from market_mcp.service import MarketService

SAMPLE = Path(__file__).resolve().parents[1] / "mcp-servers/market/src/market_mcp/resources/ecos_delivery_sample.json"


class Rows:
    """계열마다 정해 둔 행을 돌려주는 가짜 ECOS."""

    mode = "live"

    def __init__(self, rows: dict[str, list], fail: set[str] = frozenset()):
        self.rows, self.fail = rows, fail

    async def fetch(self, series, start, end):
        if series.code in self.fail:
            raise RuntimeError("down")
        return self.rows.get(series.code, [])


async def test_collect_loads_values_and_keeps_every_fetch_record(tmp_path):
    store = MemoryMarketStore()
    provider = Rows({"USD": [["20261117", "1,330.0"], ["20261118", "1338.8"]], "기준금리": [["20261118", "2.50"]]},
                    fail={"EUR"})
    result = await collect_and_load(provider, store, tmp_path / "inbox", today=date(2026, 11, 18))
    assert result["status"] == "loaded"
    assert result["detail"]["USD"]["accepted"] == 2 and result["detail"]["기준금리"]["accepted"] == 1
    assert result["detail"]["EUR"]["status"] == "error" and result["detail"]["JPY"]["status"] == "empty"
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 1  # 받은 편지함에 파일이 남는다
    assert await store.accepted("USD", date(2026, 11, 18)) == Decimal("1338.8")


async def test_sudden_move_is_held_and_correction_keeps_the_old_value(tmp_path):
    store = MemoryMarketStore()
    await collect_and_load(Rows({"USD": [["20261117", "1330"]], "기준금리": [["20261117", "2.50"]]}), store, tmp_path,
                           today=date(2026, 11, 17))
    # 환율 +5%, 기준금리 +1%p → 보류 (EXT-1)
    held = await collect_and_load(Rows({"USD": [["20261118", "1400"]], "기준금리": [["20261118", "3.50"]]}), store,
                                  tmp_path, today=date(2026, 11, 18))
    assert held["detail"]["USD"]["held"] == 1 and held["detail"]["기준금리"]["held"] == 1
    assert await store.accepted("USD", date(2026, 11, 18)) is None
    # 같은 날짜의 다른 값 → 이전 값은 정정 표시만, 새 값을 쓴다
    corrected = await collect_and_load(Rows({"USD": [["20261117", "1331"]]}), store, tmp_path, today=date(2026, 11, 18))
    assert corrected["detail"]["USD"]["corrected"] == 1
    old = [v for v in store.values if v["code"] == "USD" and v["date"] == date(2026, 11, 17)]
    assert [v["status"] for v in old] == ["superseded", "accepted"] and old[1]["value"] == Decimal("1331")
    # 같은 값은 다시 넣지 않는다
    again = await collect_and_load(Rows({"USD": [["20261117", "1331"]]}), store, tmp_path, today=date(2026, 11, 18))
    assert again["detail"]["USD"]["unchanged"] == 1 and len(store.values) == len(old) + 3


async def test_file_with_wrong_hash_is_rejected_whole(tmp_path):
    store = MemoryMarketStore()
    delivery = json.loads(SAMPLE.read_text(encoding="utf-8"))
    delivery["series"][0]["rows"][0][1] = "9999"  # 해시를 바꾸지 않고 값만 바꿈
    path = write_delivery(delivery, tmp_path)
    result = await load_file(path, store, source="replay")
    assert result["status"] == "rejected" and "해시" in result["error"] and store.values == []
    assert store.fetches[0]["status"] == "rejected"  # 거부도 기록한다
    delivery["meta"]["sha256"] = series_hash(delivery["series"])
    assert (await load_file(write_delivery(delivery, tmp_path), store, source="replay"))["status"] == "loaded"


async def test_report_reads_only_loaded_values_from_market_db(tmp_path):
    store = MemoryMarketStore()
    await collect_and_load(ReplayProvider(SAMPLE), store, tmp_path, days=400, today=date(2026, 11, 18))
    service = MarketService(DbProvider(store), today=lambda: date(2026, 11, 18))
    snapshot = await service.market_snapshot("E1001", "2026-11-18")
    usd = next(row for row in snapshot["fx"] if row["code"] == "USD")
    assert snapshot["mode"] == "db" and usd["actual_date"] <= "2026-11-18" and not usd.get("missing")
    assert any("시장 DB" in w for w in snapshot["warnings"]) and any("기록 재생" in w for w in snapshot["warnings"])


async def test_live_fetch_confirms_replayed_values(tmp_path):
    store = MemoryMarketStore()
    await collect_and_load(ReplayProvider(SAMPLE), store, tmp_path, days=10, today=date(2026, 11, 18))
    provider = DbProvider(store)
    assert any("기록 재생" in n for n in await provider.notes(date(2026, 1, 1), date(2026, 11, 18)))
    # 같은 값을 ECOS 실시간 응답으로 다시 받으면 출처가 실시간 수집으로 바뀐다 (값은 그대로)
    window = (date(2026, 11, 1), date(2026, 11, 18))
    live = {code: await store.rows(code, *window) for code in ("USD", "JPY", "EUR", "CNY", "기준금리")}
    result = await collect_and_load(Rows(live), store, tmp_path, days=20, today=date(2026, 11, 18))
    assert result["detail"]["USD"]["accepted"] == 0 and result["detail"]["USD"]["unchanged"] > 0
    assert not any("기록 재생" in n for n in await provider.notes(date(2026, 1, 1), date(2026, 11, 18)))
