"""고정 ECOS 계열만 조회한다. 자유 문장·사용자 URL·인증키는 도구 입력으로 받지 않는다."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Series:
    kind: str
    code: str
    stat: str
    item: str
    name: str
    unit: str


SERIES = (
    Series("fx", "USD", "731Y001", "0000001", "미국 달러", "원/1달러"),
    Series("fx", "JPY", "731Y001", "0000002", "일본 엔", "원/100엔"),
    Series("fx", "EUR", "731Y001", "0000003", "유로", "원/1유로"),
    Series("fx", "CNY", "731Y001", "0000053", "중국 위안", "원/1위안"),
    Series("rate", "기준금리", "722Y001", "0101000", "한국은행 기준금리", "연 %"),
)


class MarketError(ValueError):
    """사용자에게 보여도 인증키·요청 URL이 포함되지 않는 오류."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketError("ECOS 응답이 다른 주소로 이동하여 조회를 중단했습니다.")


class EcosProvider:
    mode = "live"

    def __init__(self, key: str = "", *, fetch_json=None) -> None:
        self.key = key
        self.fetch_json = fetch_json or self._fetch_json

    def _fetch_json(self, url: str) -> dict:
        # urllib는 HTTP 요청 URL을 자동 INFO 로그에 남기지 않는다. 키가 URL 경로에 있는 ECOS에 사용.
        with urllib.request.build_opener(NoRedirect()).open(url, timeout=15) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise MarketError("ECOS 응답이 허용 크기를 넘었습니다.")
        return json.loads(body)

    async def fetch(self, series: Series, start: date, end: date) -> list:
        if not self.key.strip():
            raise MarketError("MCP 서버에 ECOS_API_KEY를 설정해 주세요.")
        parts = ["api", "StatisticSearch", self.key, "json", "kr", "1", "1000", series.stat,
                 "D", start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), series.item]
        url = "https://ecos.bok.or.kr/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)
        try:
            payload = await asyncio.to_thread(self.fetch_json, url)
            result = payload.get("StatisticSearch")
            if result is None:
                if payload.get("RESULT", {}).get("CODE") == "INFO-200":
                    return []
                raise MarketError("ECOS가 조회를 거부했습니다. 인증키와 통계 서비스 상태를 확인해 주세요.")
            rows = result.get("row", [])
            if not isinstance(rows, list) or int(result.get("list_total_count", len(rows))) > 1000:
                raise MarketError("ECOS 응답 형식 또는 조회 건수를 확인해 주세요.")
            return [[r["TIME"], r["DATA_VALUE"]] for r in rows]
        except MarketError:
            raise
        except Exception:
            # 예외 본문에는 키가 든 URL이 포함될 수 있다.
            raise MarketError("ECOS에 연결하거나 응답을 읽지 못했습니다.") from None


class ReplayProvider:
    mode = "replay"

    def __init__(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            series = payload["series"]
            expected = hashlib.sha256(json.dumps(series, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if expected != payload["meta"]["sha256"]:
                raise ValueError("hash")
            allowed = {(s.kind, s.code, s.stat, s.item) for s in SERIES}
            if any((r["kind"], r["code"], r["stat"], r["item"]) not in allowed for r in series):
                raise ValueError("series")
            self.payload = payload
        except (OSError, ValueError, KeyError, TypeError):
            raise MarketError("기록 파일의 형식·해시·계열을 확인해 주세요.") from None

    async def fetch(self, series: Series, start: date, end: date) -> list:
        matches = [s for s in self.payload["series"] if s["kind"] == series.kind and s["code"] == series.code]
        if len(matches) != 1 or matches[0].get("status") != "ok":
            return []
        return matches[0]["rows"]


def _source(mode: str):
    if mode == "live":
        return EcosProvider(os.environ.get("ECOS_API_KEY", ""))
    if mode == "replay":
        sample = Path(__file__).parent / "resources" / "ecos_delivery_sample.json"
        return ReplayProvider(Path(os.environ.get("MARKET_REPLAY_FILE") or sample))
    return None


def provider_from_env():
    """리포트(market_snapshot)가 값을 읽는 곳. db = UC-02-1 이 적재한 시장 DB, live = ECOS 직접, replay = 기록 재생."""
    mode = os.environ.get("MARKET_DATA_MODE", "live").strip().lower()
    if mode == "db":
        from .collect import DbProvider, pg_store_from_env

        return DbProvider(pg_store_from_env())
    provider = _source(mode)
    if provider is None:
        raise MarketError("MARKET_DATA_MODE는 db, live, replay 중 하나여야 합니다.")
    return provider


def collect_provider_from_env():
    """수집기(UC-02-1)가 받아 올 곳. MARKET_COLLECT_MODE 가 없으면 ECOS_API_KEY 가 있을 때 live, 없으면 replay."""
    default = "live" if os.environ.get("ECOS_API_KEY", "").strip() else "replay"
    mode = os.environ.get("MARKET_COLLECT_MODE", default).strip().lower() or default
    provider = _source(mode)
    if provider is None:
        raise MarketError("MARKET_COLLECT_MODE는 live 또는 replay여야 합니다.")
    return provider
