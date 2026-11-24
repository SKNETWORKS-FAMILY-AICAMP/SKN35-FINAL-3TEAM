"""ECOS → 날짜/수치 검증 → 스냅샷 → 요약 검사 → 소유자별 리포트.

저장은 store 에 맡긴다 (write(kind, payload) / read(kind, ident, owner)).
- 시장 MCP 서버: store 없음. market_snapshot 만 열고 아무것도 저장하지 않는다 (데이터 조회 전용).
- 플랫폼 API: 이 클래스를 라이브러리로 써서 양식 검색·리포트 작성·저장을 하고, store 는 API 문서 저장소다
  (apps/api/src/tools/market.py). 모든 서류의 저장·내려받기를 API 한 곳에 모은다.
- FileStore: 파일 저장 (테스트·단독 실행용).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .ecos import SERIES, MarketError
from .report_templates import ReportTemplateCatalog, render_template
from .report_documents import fill_document, report_values, document_text

SOURCE = "한국은행 경제통계시스템(ECOS)"
FOOTER = "시장 기준 통계이며 개별 은행이 고시하는 고객 환율·금리와 다릅니다."
FORBIDDEN = ("전망", "예상", "것으로 보", "때문", "우려", "기대", "추천", "매수", "매도")


def canonical_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise MarketError("기준일은 YYYY-MM-DD 형식으로 입력해 주세요.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise MarketError("유효한 기준일을 입력해 주세요.") from None


def number(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def clean_rows(rows: list, start: date, end: date, kind: str) -> tuple[list, list]:
    unique = {}
    for row in rows:
        try:
            raw, value = row
            when = date.fromisoformat(raw) if "-" in raw else datetime.strptime(raw, "%Y%m%d").date()
            amount = Decimal(str(value).replace(",", ""))
            if not amount.is_finite() or amount < 0 or (kind == "fx" and amount == 0) or not start <= when <= end:
                continue
        except (ValueError, TypeError, InvalidOperation):
            continue
        if when in unique and unique[when] != amount:
            raise MarketError("같은 기준일에 서로 다른 수치가 있어 사용할 수 없습니다.")
        unique[when] = amount
    accepted, held = [], []
    for when, amount in sorted(unique.items()):
        if accepted:
            previous = accepted[-1][1]
            excessive = (abs(amount - previous) / previous * 100 > 3) if kind == "fx" else abs(amount - previous) > Decimal(".5")
            if excessive:
                held.append({"date": when.isoformat(), "value": number(amount)})
                continue
        accepted.append((when, amount))
    return accepted, held


def snapshot_parts(snapshot: dict) -> dict[str, str]:
    lines = ["| 통화 | 환율 | 단위 | 실제 기준일 | 직전 관측일 | 증감 | 증감률 |",
              "|---|---:|---|---|---|---:|---:|"]
    for row in snapshot["fx"]:
        if row.get("missing"):
            lines.append(f"| {row['code']} | 값 없음 | {row['unit']} | — | — | — | — |")
            continue
        change = f"{row['change']:+,.2f}" if "change" in row else "—"
        pct = f"{row['change_pct']:+.2f}%" if "change_pct" in row else "—"
        lines.append(f"| {row['code']} | {row['value']:,.2f} | {row['unit']} | {row['actual_date']} | {row.get('prev_date', '—')} | {change} | {pct} |")
    fx_table = "\n".join(lines)
    lines = []
    for row in snapshot["rates"]:
        if row.get("missing"):
            lines += ["- 한국은행 기준금리: 값 없음"]
        else:
            since = "수집 범위 내 최초 확인일" if row["since_is_range_start"] else "같은 값이 관측된 시작일"
            lines += [f"- {row['value']:.2f}% · 실제 기준일 {row['actual_date']} · {since} {row['observed_since']}"]
    rates = "\n".join(lines) or "- 해당 양식에서 기준금리를 사용하지 않습니다."
    quality = ["| 항목 | 요청 기준일 | 실제 관측일 | 확인 상태 |", "|---|---|---|---|"]
    for row in snapshot["fx"] + snapshot["rates"]:
        state = "자료 없음" if row.get("missing") else "요청일과 관측일 다름" if row.get("stale") else "요청일 자료 확인"
        quality.append(f"| {row['code']} | {snapshot['base_date']} | {row.get('actual_date', '—')} | {state} |")
    notes = ["**기록 데이터 재생 — 실시간 조회가 아닙니다.**"] if snapshot["mode"] == "replay" else []
    notes += [f"- 안내: {warning}" for warning in snapshot["warnings"]]
    notes += [f"출처: {SOURCE}", f"조회 시각(UTC): {snapshot['fetched_at']}", FOOTER]
    return {"fx_table": fx_table, "rates": rates, "data_quality": "\n".join(quality), "source_notes": "\n\n".join(notes)}


def snapshot_markdown(snapshot: dict) -> str:
    parts = snapshot_parts(snapshot)
    return f"## 금리·환율 조회 · 요청 기준일 {snapshot['base_date']}\n\n{parts['fx_table']}\n\n### 기준금리\n{parts['rates']}\n\n{parts['source_notes']}"


def check_summary(summary: str, snapshot: dict) -> dict:
    allowed = {Decimal(100), Decimal(1)}  # 표의 100엔·1달러/1유로/1위안 단위
    signed = set(allowed)
    dates = [snapshot["base_date"]]
    for row in snapshot["fx"] + snapshot["rates"]:
        for key in ("value", "prev_value", "change", "change_pct"):
            if row.get(key) is not None:
                value = Decimal(str(row[key]))
                allowed.add(abs(value))
                signed.add(value)
        dates += [row[k] for k in ("actual_date", "prev_date", "observed_since") if row.get(k)]
    for value in dates:
        allowed.update(Decimal(part) for part in value.split("-"))
        signed.update(Decimal(part) for part in value.split("-"))
    actual_count = sum(not row.get("missing") for row in snapshot["fx"])
    wrong_counts = []
    def check_currency_count(match):
        raw = match[1]
        if int(raw) != actual_count:
            wrong_counts.append(raw)
        return match[0].replace(raw, "", 1)
    numeric_summary = re.sub(r"(\d+)\s*(?:개|종)(?:의)?\s*(?:주요\s*)?통화", check_currency_count, summary)
    bad = wrong_counts + [token for token in re.findall(r"(?<!\d)[+-]?\d[\d,]*(?:\.\d+)?", numeric_summary)
           if not any(abs(Decimal(token.replace(',', '')) - value) < Decimal('.005')
                      for value in (signed if token.startswith(('+', '-')) else allowed))]
    forbidden = [word for word in FORBIDDEN if word in summary]
    if "{{" in summary or "}}" in summary:
        forbidden.append("요약에 미작성 입력 위치가 남음")
    if any(row.get("stale") for row in snapshot["fx"] + snapshot["rates"]) and re.search(r"이후[^.!?\n]*변동[^.!?\n]*(?:없|않)", summary):
        forbidden.append("관측되지 않은 기간의 변동 여부 단정")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", summary.strip()) if s.strip()]
    return {"passed": bool(summary.strip()) and not bad and not forbidden and len(sentences) <= 3,
            "numbers_not_in_table": bad, "forbidden_words": forbidden, "sentences": len(sentences)}


class FileStore:
    """소유자별 JSON 파일 저장소 (테스트·단독 실행용)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, kind: str, payload: dict) -> None:
        directory = self.root / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (payload["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def read(self, kind: str, ident: str, owner: str) -> dict:
        try:
            payload = json.loads((self.root / kind / (ident + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MarketError("열람 가능한 시장 자료를 찾을 수 없습니다.") from None
        if payload.get("owner_user_id") != owner:
            raise MarketError("열람 가능한 시장 자료를 찾을 수 없습니다.")
        return payload


class MarketService:
    def __init__(self, provider, root: Path | None = None, *, store=None, today=None,
                 templates_dir: Path | None = None) -> None:
        self.provider, self.root = provider, root
        self.store = store if store is not None else (FileStore(root) if root is not None else None)
        self.today = today or (lambda: datetime.now(ZoneInfo("Asia/Seoul")).date())
        self.templates = ReportTemplateCatalog(templates_dir or Path(os.environ.get("REPORT_TEMPLATES_DIR", "config/report_templates")))
        self.default_template = os.environ.get("REPORT_DEFAULT_TEMPLATE", "market-daily")

    async def search_report_templates(self, requester_id: str, query: str = "") -> dict:
        self._owner(requester_id)
        matches = await asyncio.to_thread(self.templates.search, query)
        lines = ["## 사용 가능한 보고서 양식", "테스트용 예시 양식이며 실제 은행 공식 서식이 아닙니다."]
        lines += [f"- **{m['name']}** · 버전 {m['version']} — {m['description']}" for m in matches]
        if not matches:
            lines += ["요청에 맞는 양식을 찾지 못했습니다."]
        return {"matches": matches, "search_backend": "local_keywords", "markdown": "\n\n".join(lines)}

    async def get_report_template(self, requester_id: str, template_id: str, expected_hash: str | None = None) -> dict:
        self._owner(requester_id)
        template = await asyncio.to_thread(self.templates.get, template_id, expected_hash)
        selection = {"id": uuid4().hex, "owner_user_id": requester_id, "template": template}
        await self._write("template_selections", selection)
        return {"template_selection_id": selection["id"], "template": template,
                "markdown": f"## {template['name']} · 버전 {template['version']}\n\n{template['description']}\n\n" +
                ("\n".join(s.get("text", "") or s.get("label", "") or s.get("binding", "") for s in template["sections"])
                 if "sections" in template else template["body_markdown"])}

    def _owner(self, owner: str) -> str:
        if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", owner):
            raise MarketError("로그인 사용자 정보를 확인할 수 없습니다.")
        return owner

    async def _write(self, kind: str, payload: dict) -> None:
        """저장소가 async(플랫폼 문서 저장소)면 기다리고, 파일 저장소면 스레드에서 쓴다."""
        if self.store is None:
            return  # 시장 MCP 서버는 저장하지 않는다
        if inspect.iscoroutinefunction(self.store.write):
            await self.store.write(kind, payload)
        else:
            await asyncio.to_thread(self.store.write, kind, payload)

    async def _read(self, kind: str, ident: str, owner: str) -> dict:
        self._owner(owner)
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident) or self.store is None:
            raise MarketError("열람 가능한 시장 자료를 찾을 수 없습니다.")
        if inspect.iscoroutinefunction(self.store.read):
            return await self.store.read(kind, ident, owner)
        return await asyncio.to_thread(self.store.read, kind, ident, owner)

    async def market_snapshot(self, requester_id: str, base_date: str | None = None) -> dict:
        self._owner(requester_id)
        end = canonical_date(base_date) if base_date else self.today()
        if end > self.today():
            raise MarketError("미래 기준일의 시장 수치를 조회할 수 없습니다.")
        start = end - timedelta(days=400)
        results = await asyncio.gather(*(self.provider.fetch(s, start, end) for s in SERIES), return_exceptions=True)
        fx, rates, warnings = [], [], []
        if self.provider.mode == "replay":
            warnings.append("전달받은 기록 파일을 재생한 결과입니다. ECOS 실시간 응답으로 검증한 값이 아닙니다.")
        if hasattr(self.provider, "notes"):
            warnings += await self.provider.notes(start, end)
        for series, result in zip(SERIES, results):
            row = {"code": series.code, "name": series.name, "unit": series.unit}
            accepted, held = [], []
            if isinstance(result, Exception):
                warnings.append(f"{series.code}: " + (str(result) if isinstance(result, MarketError) else "자료 조회 실패"))
            else:
                accepted, held = clean_rows(result, start, end, series.kind)
            if held:
                warnings.append(f"{series.code}: 급변 수치 {len(held)}건 보류. 보류 값은 표에 사용하지 않았습니다.")
            if not accepted:
                row["missing"] = True
            else:
                when, value = accepted[-1]
                row.update(value=number(value), actual_date=when.isoformat(), stale=when != end)
                if when != end:
                    warnings.append(f"{series.code}: 요청일 값이 없어 실제 기준일 {when.isoformat()} 값을 사용했습니다.")
                if series.kind == "fx" and len(accepted) > 1:
                    previous_date, previous = accepted[-2]
                    diff = value - previous
                    row.update(prev_date=previous_date.isoformat(), prev_value=number(previous), change=number(diff),
                               change_pct=number(diff / previous * 100))
                if series.kind == "rate":
                    since, previous = when, None
                    for observed, amount in reversed(accepted):
                        if amount != value:
                            previous = amount
                            break
                        since = observed
                    row.update(observed_since=since.isoformat(), since_is_range_start=previous is None)
                    if previous is not None:
                        row["prev_value"] = number(previous)
            (fx if series.kind == "fx" else rates).append(row)
        if all(row.get("missing") for row in fx + rates):
            raise MarketError("시장 수치를 조회하지 못했습니다. MCP 서버의 인증키·연결 상태·조회 기간을 확인해 주세요.")
        snapshot = {"id": uuid4().hex, "owner_user_id": requester_id, "base_date": end.isoformat(),
                    "mode": self.provider.mode, "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "fx": fx, "rates": rates, "warnings": warnings}
        snapshot["markdown"] = snapshot_markdown(snapshot)
        await self._write("snapshots", snapshot)
        return {**{k: v for k, v in snapshot.items() if k != "owner_user_id"}, "snapshot_id": snapshot["id"]}

    async def save_morning_report(self, requester_id: str, snapshot_id: str, summary: str = "",
                                  template_selection_id: str | None = None) -> dict:
        if not isinstance(summary, str) or len(summary) > 2000:
            raise MarketError("요약은 2,000자 이내의 문자열이어야 합니다.")
        snapshot = await self._read("snapshots", snapshot_id, requester_id)
        if template_selection_id:
            selection = await self._read("template_selections", template_selection_id, requester_id)
            template = selection["template"]
            # 내용은 읽은 버전으로 고정하되, 관리자가 사용 중단/삭제한 양식으로 새 보고서를 만들지는 않는다.
            await asyncio.to_thread(self.templates.get, template["id"])
        else:
            template = await asyncio.to_thread(self.templates.get, self.default_template)
        # 간단 환율 양식처럼 금리를 사용하지 않는 양식에서는 요약에도 그 자료를 넣지 않는다.
        scoped = snapshot
        if "rates" not in template["bindings"] and "data_quality" not in template["bindings"]:
            scoped = {**snapshot, "rates": [], "warnings": [w for w in snapshot["warnings"] if not w.startswith("기준금리:")]}
        check = check_summary(summary, scoped)
        report = {"id": uuid4().hex, "owner_user_id": requester_id, "snapshot_id": snapshot_id,
                  "base_date": snapshot["base_date"], "mode": snapshot["mode"], "check": check,
                  "summary": summary if check["passed"] else None, "summary_submitted": summary,
                  "created_at": datetime.now(timezone.utc).isoformat(), "snapshot": {k: v for k, v in snapshot.items() if k != "markdown"},
                  "template": template}
        note = "요약 검사 통과" if check["passed"] else "요약이 비어 있거나 검사를 통과하지 못해 양식의 나머지 항목과 출처만 저장했습니다."
        available = {"base_date", "source_notes", "data_quality", "prepared_by", "report_id"}
        if any(not row.get("missing") for row in snapshot["fx"]):
            available.add("fx_table")
        if any(not row.get("missing") for row in scoped["rates"]):
            available.add("rates")
        if check["passed"]:
            available.add("summary")
        if "sections" in template:
            values = report_values(scoped, summary=summary if check["passed"] else note,
                                   prepared_by=requester_id, report_id=report["id"])
            report["document"] = fill_document(template, values, available)
            report["document"]["blocks"].append({"type": "paragraph", "text": note})
        else:
            # 이미 선택된 이전 양식은 당시 형식 그대로 처리한다.
            bindings = {**snapshot_parts(scoped), "base_date": snapshot["base_date"],
                        "summary": summary if check["passed"] else note, "prepared_by": requester_id, "report_id": report["id"]}
            report["markdown"] = render_template(template, bindings, available) + "\n\n" + note
        await self._write("reports", report)
        return {"report_id": report["id"], "base_date": report["base_date"], "mode": report["mode"],
                "check": check, "template": {key: template[key] for key in ("id", "name", "version", "hash")},
                **({"document": report["document"], "text": document_text(report["document"])}
                   if "document" in report else {"markdown": report["markdown"]})}

    async def get_morning_report(self, requester_id: str, report_id: str) -> dict:
        report = await self._read("reports", report_id, requester_id)
        return {"report_id": report["id"], "base_date": report["base_date"], "mode": report["mode"],
                "check": report["check"], "template": report.get("template"),
                **({"document": report["document"], "text": document_text(report["document"])}
                   if "document" in report else {"markdown": report["markdown"]})}
