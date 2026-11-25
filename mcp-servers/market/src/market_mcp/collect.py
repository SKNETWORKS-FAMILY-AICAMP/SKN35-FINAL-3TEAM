"""시장 데이터 수집기 + 적재기 (UC-02-1). 에이전트가 아니라 예약 실행(평일 09:10)이나 명령으로 돈다.

1. 수집기: ECOS(또는 기록 재생)에서 USD·JPY·EUR·CNY 환율과 기준금리를 받는다 (계열은 ecos.SERIES 로 고정).
2. 받은 내용과 해시(SHA-256)를 받은 편지함(경계) 폴더에 파일로 놓는다.
3. 적재기가 파일의 해시와 형식을 검사한다. 해시가 다르면 파일 전체를 거부한다 (3a).
4. 통과한 값만 시장 DB 에 넣는다.
   - 직전 관측값보다 환율이 3% 넘게, 기준금리가 0.5%p 넘게 움직이면 보류한다 (4a, EXT-1).
   - 같은 날짜에 이미 다른 값이 있으면 고치지 않고, 이전 값에 정정 표시(superseded)를 하고 새 값을 넣는다.
받은 기록(성공·빈 응답·오류·거부)은 모두 market_fetch 에 남는다.

명령: uv run --package market-mcp market-collect [--days 10]   (DATABASE_URL·ECOS_API_KEY 는 .env)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .ecos import SERIES, MarketError, Series

FX_LIMIT_PCT = Decimal("3")  # EXT-1: 환율 변동률 한도 (%)
RATE_LIMIT_PT = Decimal("0.5")  # EXT-1: 기준금리 변동 한도 (%p)
COLLECTOR = "market_mcp.collect v1"


def series_hash(series: list) -> str:
    return hashlib.sha256(json.dumps(series, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def fetch_delivery(provider, start: date, end: date) -> dict:
    """계열마다 받아 하나의 전달 묶음으로. 계열 하나가 실패해도 나머지는 받는다 (실패도 기록한다)."""
    results = await asyncio.gather(*(provider.fetch(s, start, end) for s in SERIES), return_exceptions=True)
    series = []
    for spec, result in zip(SERIES, results):
        item = {"kind": spec.kind, "code": spec.code, "stat": spec.stat, "item": spec.item, "unit": spec.unit}
        if isinstance(result, Exception):
            item.update(status="error", message=str(result) if isinstance(result, MarketError) else "조회 실패", rows=[])
        else:
            item.update(status="ok" if result else "empty", message="", rows=[[str(r[0]), str(r[1])] for r in result])
        series.append(item)
    return {"meta": {"collector": COLLECTOR, "source": provider.mode,
                     "requested": {"start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d")},
                     "fetched_at": datetime.now(timezone.utc).isoformat(), "sha256": series_hash(series)},
            "series": series}


def write_delivery(delivery: dict, inbox: Path) -> Path:
    """받은 편지함에 놓는다. 다 쓴 뒤 이름을 바꿔 반쪽 파일이 적재되지 않게 한다."""
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = inbox / f"ecos_{stamp}.json"
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(delivery, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)
    return path


def check_delivery(raw: bytes) -> dict:
    """해시·형식·계열 검사. 통과하지 못하면 MarketError (파일 전체 거부)."""
    try:
        delivery = json.loads(raw.decode("utf-8-sig"))
        series = delivery["series"]
        if series_hash(series) != delivery["meta"]["sha256"]:
            raise MarketError("받은 파일의 해시가 맞지 않아 거부했습니다.")
        allowed = {(s.kind, s.code, s.stat, s.item) for s in SERIES}
        for item in series:
            if (item["kind"], item["code"], item["stat"], item["item"]) not in allowed:
                raise MarketError("허용하지 않은 계열이 있어 거부했습니다.")
            if item.get("status") not in ("ok", "empty", "error") or not isinstance(item.get("rows"), list):
                raise MarketError("계열 형식이 맞지 않아 거부했습니다.")
        delivery["meta"]["requested"]["start"], delivery["meta"]["requested"]["end"]  # noqa: B018 (있어야 한다)
    except MarketError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise MarketError("받은 파일의 형식이 맞지 않아 거부했습니다.") from None
    return delivery


def _parse(rows: list) -> list[tuple[date, Decimal]]:
    """형식이 맞는 행만 (날짜, 값). 같은 날짜가 서로 다른 값으로 두 번 오면 그 날짜는 쓰지 않는다."""
    seen: dict[date, Decimal | None] = {}
    for row in rows:
        try:
            raw, value = row
            when = date.fromisoformat(raw) if "-" in raw else datetime.strptime(raw, "%Y%m%d").date()
            amount = Decimal(str(value).replace(",", ""))
            if not amount.is_finite() or amount <= 0:
                continue
        except (ValueError, TypeError, InvalidOperation):
            continue
        seen[when] = amount if when not in seen or seen[when] == amount else None
    return sorted((when, amount) for when, amount in seen.items() if amount is not None)


def excessive(spec: Series, previous: Decimal, value: Decimal) -> bool:
    if spec.kind == "fx":
        return abs(value - previous) / previous * 100 > FX_LIMIT_PCT
    return abs(value - previous) > RATE_LIMIT_PT


def _requested(meta: dict) -> tuple[date, date]:
    start, end = meta["requested"]["start"], meta["requested"]["end"]
    return datetime.strptime(start, "%Y%m%d").date(), datetime.strptime(end, "%Y%m%d").date()


async def load_file(path: Path, store, *, source: str) -> dict:
    """받은 편지함의 파일 하나를 적재한다. 결과 요약을 돌려준다 (거부도 기록하고 돌려준다)."""
    raw = await asyncio.to_thread(path.read_bytes)
    sha = hashlib.sha256(raw).hexdigest()
    try:
        delivery = check_delivery(raw)
        start, end = _requested(delivery["meta"])
    except MarketError as error:
        today = date.today()
        fetch_id = await store.start_fetch(source, today, today, str(path), sha)
        await store.finish_fetch(fetch_id, "rejected", {}, str(error))
        return {"fetch_id": fetch_id, "status": "rejected", "error": str(error)}
    source_of = delivery["meta"].get("source", source)
    fetch_id = await store.start_fetch(source_of, start, end, str(path), sha)
    detail: dict[str, dict] = {}
    try:
        for item in delivery["series"]:
            spec = next(s for s in SERIES if s.code == item["code"] and s.kind == item["kind"])
            counts = {"status": item["status"], "accepted": 0, "held": 0, "corrected": 0, "unchanged": 0}
            if item["status"] == "error":
                counts["message"] = item.get("message", "")
            for when, value in _parse(item["rows"]) if item["status"] == "ok" else []:
                current = await store.accepted(spec.code, when)
                if current is not None and current == value:
                    counts["unchanged"] += 1
                    if source_of == "live":  # 기록 재생으로 넣은 값을 ECOS 실시간 응답이 같은 값으로 확인
                        await store.confirm(spec.code, when, fetch_id)
                    continue
                previous = await store.previous(spec.code, when)
                if previous is not None and excessive(spec, previous, value):
                    await store.insert(spec.code, when, value, "held", fetch_id,
                                       f"직전 관측값 {previous} 대비 급변 (보류)")
                    counts["held"] += 1
                    continue
                if current is not None:
                    await store.supersede(spec.code, when)
                    await store.insert(spec.code, when, value, "accepted", fetch_id, f"정정: 이전 값 {current}")
                    counts["corrected"] += 1
                else:
                    await store.insert(spec.code, when, value, "accepted", fetch_id, None)
                    counts["accepted"] += 1
            detail[spec.code] = counts
    except Exception as error:
        await store.finish_fetch(fetch_id, "error", detail, type(error).__name__)
        raise
    await store.finish_fetch(fetch_id, "loaded", detail, None)
    return {"fetch_id": fetch_id, "status": "loaded", "detail": detail}


async def collect_and_load(provider, store, inbox: Path, *, days: int = 10, today: date | None = None) -> dict:
    """예약 실행 한 번: 최근 days 일을 받아 받은 편지함에 놓고 적재한다."""
    end = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    start = end - timedelta(days=days)
    delivery = await fetch_delivery(provider, start, end)
    path = await asyncio.to_thread(write_delivery, delivery, inbox)
    return await load_file(path, store, source=provider.mode)


class MemoryMarketStore:
    """테스트용 시장 DB (PgMarketStore 와 같은 메서드)."""

    def __init__(self) -> None:
        self.fetches: list[dict] = []
        self.values: list[dict] = []

    async def start_fetch(self, source, start, end, file_path, sha) -> int:
        self.fetches.append({"id": len(self.fetches) + 1, "source": source, "status": "loading", "sha256": sha,
                             "requested": (start, end), "file_path": file_path})
        return len(self.fetches)

    async def finish_fetch(self, fetch_id, status, detail, error) -> None:
        self.fetches[fetch_id - 1].update(status=status, detail=detail, error=error)

    async def accepted(self, code, when) -> Decimal | None:
        return next((v["value"] for v in self.values if v["code"] == code and v["date"] == when
                     and v["status"] == "accepted"), None)

    async def previous(self, code, when) -> Decimal | None:
        rows = [v for v in self.values if v["code"] == code and v["date"] < when and v["status"] == "accepted"]
        return max(rows, key=lambda v: v["date"])["value"] if rows else None

    async def insert(self, code, when, value, status, fetch_id, note) -> None:
        self.values.append({"code": code, "date": when, "value": value, "status": status, "fetch_id": fetch_id, "note": note})

    async def confirm(self, code, when, fetch_id) -> None:
        replay = {f["id"] for f in self.fetches if f["source"] == "replay"}
        for v in self.values:
            if v["code"] == code and v["date"] == when and v["status"] == "accepted" and v["fetch_id"] in replay:
                v["fetch_id"] = fetch_id

    async def supersede(self, code, when) -> None:
        for v in self.values:
            if v["code"] == code and v["date"] == when and v["status"] == "accepted":
                v["status"] = "superseded"

    async def rows(self, code, start, end) -> list:
        return [[v["date"].strftime("%Y%m%d"), str(v["value"])] for v in sorted(self.values, key=lambda v: v["date"])
                if v["code"] == code and start <= v["date"] <= end and v["status"] == "accepted"]

    async def sources(self, start, end) -> set[str]:
        ids = {v["fetch_id"] for v in self.values if v["status"] == "accepted" and start <= v["date"] <= end}
        return {f["source"] for f in self.fetches if f["id"] in ids}


class PgMarketStore:
    """PostgreSQL 시장 DB (마이그레이션 0014). sessions 는 비동기 SQLAlchemy 세션을 여는 함수."""

    def __init__(self, sessions) -> None:
        self._sessions = sessions

    async def _scalar(self, sql: str, args: dict, commit: bool = False):
        from sqlalchemy import text

        async with self._sessions() as s:
            value = await s.scalar(text(sql), args)
            if commit:
                await s.commit()
        return value

    async def _execute(self, sql: str, args: dict) -> None:
        from sqlalchemy import text

        async with self._sessions() as s:
            await s.execute(text(sql), args)
            await s.commit()

    async def start_fetch(self, source, start, end, file_path, sha) -> int:
        return await self._scalar("""INSERT INTO market_fetch (source, requested_start, requested_end, file_path, sha256, status)
            VALUES (:s, :a, :b, :f, :h, 'loading') RETURNING id""",
                                  {"s": source, "a": start, "b": end, "f": file_path, "h": sha}, commit=True)

    async def finish_fetch(self, fetch_id, status, detail, error) -> None:
        await self._execute("""UPDATE market_fetch SET status = :s, detail = CAST(:d AS JSONB), error = :e, loaded_at = now()
            WHERE id = :i""", {"s": status, "d": json.dumps(detail, ensure_ascii=False), "e": error, "i": fetch_id})

    async def accepted(self, code, when) -> Decimal | None:
        return await self._scalar("""SELECT value FROM market_value WHERE series_code = :c AND obs_date = :d
            AND status = 'accepted'""", {"c": code, "d": when})

    async def previous(self, code, when) -> Decimal | None:
        return await self._scalar("""SELECT value FROM market_value WHERE series_code = :c AND obs_date < :d
            AND status = 'accepted' ORDER BY obs_date DESC LIMIT 1""", {"c": code, "d": when})

    async def insert(self, code, when, value, status, fetch_id, note) -> None:
        await self._execute("""INSERT INTO market_value (series_code, obs_date, value, status, fetch_id, note)
            VALUES (:c, :d, :v, :s, :f, :n)""", {"c": code, "d": when, "v": value, "s": status, "f": fetch_id, "n": note})

    async def confirm(self, code, when, fetch_id) -> None:
        """기록 재생으로 넣은 값을 실시간 응답이 같은 값으로 확인하면 출처만 실시간 수집으로 바꾼다 (값은 그대로)."""
        await self._execute("""UPDATE market_value v SET fetch_id = :f, note = '실시간 응답으로 같은 값 확인'
            FROM market_fetch old WHERE old.id = v.fetch_id AND old.source = 'replay'
            AND v.series_code = :c AND v.obs_date = :d AND v.status = 'accepted'""", {"c": code, "d": when, "f": fetch_id})

    async def supersede(self, code, when) -> None:
        await self._execute("""UPDATE market_value SET status = 'superseded', superseded_at = now()
            WHERE series_code = :c AND obs_date = :d AND status = 'accepted'""", {"c": code, "d": when})

    async def rows(self, code, start, end) -> list:
        from sqlalchemy import text

        async with self._sessions() as s:
            result = (await s.execute(text("""SELECT obs_date, value FROM market_value WHERE series_code = :c
                AND obs_date BETWEEN :a AND :b AND status = 'accepted' ORDER BY obs_date"""),
                                      {"c": code, "a": start, "b": end})).all()
        return [[r.obs_date.strftime("%Y%m%d"), format(r.value.normalize(), "f")] for r in result]

    async def sources(self, start, end) -> set[str]:
        """기간 안 accepted 값이 어디서 왔는지 (live·replay)."""
        from sqlalchemy import text

        async with self._sessions() as s:
            result = (await s.execute(text("""SELECT DISTINCT f.source FROM market_value v JOIN market_fetch f ON f.id = v.fetch_id
                WHERE v.status = 'accepted' AND v.obs_date BETWEEN :a AND :b"""), {"a": start, "b": end})).all()
        return {r.source for r in result}

    async def last_fetch(self) -> dict | None:
        from sqlalchemy import text

        async with self._sessions() as s:
            row = (await s.execute(text("SELECT id, source, status, detail, error, fetched_at FROM market_fetch "
                                        "ORDER BY id DESC LIMIT 1"))).first()
        return dict(row._mapping) if row else None


class DbProvider:
    """시장 DB 의 accepted 값으로 조회한다 (MARKET_DATA_MODE=db). UC-02 는 UC-02-1 이 넣은 값만 쓴다."""

    mode = "db"

    def __init__(self, store) -> None:
        self.store = store

    async def fetch(self, series: Series, start: date, end: date) -> list:
        return await self.store.rows(series.code, start, end)

    async def notes(self, start: date, end: date) -> list[str]:
        """표에 쓰이는 최근 값(기준일 전 2주)이 기록 재생에서 왔으면 알린다."""
        notes = ["사내 시장 DB에 적재된 값입니다 (UC-02-1 수집·검사를 통과한 값만)."]
        if "replay" in await self.store.sources(max(start, end - timedelta(days=14)), end):
            notes.append("시장 DB 값 중 기록 재생으로 적재한 값이 있습니다. ECOS 실시간 응답으로 검증한 값이 아닙니다.")
        return notes


def pg_store_from_env() -> PgMarketStore:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise MarketError("DATABASE_URL 이 없어 시장 DB 에 연결할 수 없습니다.")
    return PgMarketStore(async_sessionmaker(create_async_engine(url, pool_pre_ping=True), expire_on_commit=False))


def inbox_from_env() -> Path:
    return Path(os.environ.get("MARKET_INBOX_DIR", "data/market_inbox"))


def main() -> None:
    """명령으로 한 번 수집·적재한다 (예약 실행이 하는 일과 같다)."""
    from .ecos import collect_provider_from_env

    parser = argparse.ArgumentParser(description="ECOS 금리·환율을 받아 시장 DB 에 적재 (UC-02-1)")
    parser.add_argument("--days", type=int, default=10, help="오늘부터 며칠 전까지 받을지 (기본 10)")
    parser.add_argument("--file", type=Path, help="받은 편지함 파일 하나만 적재 (수집은 하지 않음)")
    args = parser.parse_args()
    store = pg_store_from_env()

    async def run() -> dict:
        if args.file:
            return await load_file(args.file, store, source="replay")
        return await collect_and_load(collect_provider_from_env(), store, inbox_from_env(), days=args.days)

    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=1, default=str))
