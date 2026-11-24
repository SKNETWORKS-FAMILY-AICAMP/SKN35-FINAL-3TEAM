"""시장 DB (UC-02-1, 마이그레이션 0014) — 실제 DB. 없으면 건너뜀. 바깥 트랜잭션 안에서 하고 되돌린다."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from market_mcp.collect import DbProvider, PgMarketStore, collect_and_load
from tests.db.test_db_stores import db  # noqa: F401
from tests.test_market_collect import Rows


async def test_pg_market_store_holds_corrects_and_serves_rows(db, tmp_path):  # noqa: F811
    if not await db["conn"].scalar(text("SELECT to_regclass('market_value') IS NOT NULL")):
        pytest.skip("마이그레이션 0014 전 DB")
    store = PgMarketStore(db["sessions"])
    code = "USD"
    base = await store.accepted(code, date(2099, 1, 1))
    assert base is None
    first = await collect_and_load(Rows({code: [["20990101", "1330"]]}), store, tmp_path, today=date(2099, 1, 1))
    held = await collect_and_load(Rows({code: [["20990102", "1500"]]}), store, tmp_path, today=date(2099, 1, 2))
    fixed = await collect_and_load(Rows({code: [["20990101", "1331"]]}), store, tmp_path, today=date(2099, 1, 2))
    assert first["detail"][code]["accepted"] == 1 and held["detail"][code]["held"] == 1 and fixed["detail"][code]["corrected"] == 1
    assert await store.rows(code, date(2099, 1, 1), date(2099, 1, 2)) == [["20990101", "1331"]]
    statuses = (await db["conn"].execute(text("SELECT status FROM market_value WHERE series_code = :c AND obs_date >= '2099-01-01' "
                                              "ORDER BY id"), {"c": code})).scalars().all()
    assert statuses == ["superseded", "held", "accepted"]
    fetch = (await db["conn"].execute(text("SELECT status, detail FROM market_fetch WHERE id = :i"), {"i": fixed["fetch_id"]})).first()
    assert fetch.status == "loaded" and fetch.detail[code]["corrected"] == 1
    assert await DbProvider(store).fetch(type("S", (), {"code": code})(), date(2099, 1, 1), date(2099, 1, 1)) == [["20990101", "1331"]]
    assert await store.previous(code, date(2099, 1, 5)) == Decimal("1331")
