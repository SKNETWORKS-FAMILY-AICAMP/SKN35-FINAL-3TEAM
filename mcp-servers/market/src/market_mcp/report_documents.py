"""보고서의 원본은 구조화 JSON. 화면/내보내기는 이 원본의 투영이다."""
from .ecos import MarketError


def paragraph(text):
    return {"type": "paragraph", "text": text}


def report_values(snapshot, *, summary, prepared_by, report_id):
    fx_rows = []
    for row in snapshot["fx"]:
        fx_rows.append([row["code"], "값 없음" if row.get("missing") else f"{row['value']:,.2f}",
                        row["unit"], row.get("actual_date", "—"), row.get("prev_date", "—"),
                        f"{row['change']:+,.2f}" if "change" in row else "—",
                        f"{row['change_pct']:+.2f}%" if "change_pct" in row else "—"])
    rates = []
    for row in snapshot["rates"]:
        if row.get("missing"):
            rates.append("한국은행 기준금리: 값 없음")
        else:
            since = "수집 범위 내 최초 확인일" if row["since_is_range_start"] else "같은 값이 관측된 시작일"
            rates.append(f"{row['value']:.2f}% · 실제 기준일 {row['actual_date']} · {since} {row['observed_since']}")
    quality = [[row["code"], snapshot["base_date"], row.get("actual_date", "—"),
                "자료 없음" if row.get("missing") else "요청일과 관측일 다름" if row.get("stale") else "요청일 자료 확인"]
               for row in snapshot["fx"] + snapshot["rates"]]
    notes = ["기록 데이터 재생 — 실시간 조회가 아닙니다."] if snapshot["mode"] == "replay" else []
    notes += snapshot["warnings"] + ["출처: 한국은행 경제통계시스템(ECOS)",
             "조회 시각(UTC): " + snapshot["fetched_at"], "시장 기준 통계이며 개별 은행이 고시하는 고객 환율·금리와 다릅니다."]
    return {
        "base_date": snapshot["base_date"], "prepared_by": prepared_by, "report_id": report_id,
        "summary": paragraph(summary),
        "fx_table": {"type": "table", "columns": ["통화", "환율", "단위", "실제 기준일", "직전 관측일", "증감", "증감률"], "rows": fx_rows},
        "rates": {"type": "list", "items": rates or ["해당 양식에서 기준금리를 사용하지 않습니다."]},
        "data_quality": {"type": "table", "columns": ["항목", "요청 기준일", "실제 관측일", "확인 상태"], "rows": quality},
        "source_notes": {"type": "list", "items": notes},
    }


def fill_document(template, values, available):
    missing = set(template["required_bindings"]) - available
    if missing:
        raise MarketError("보고서 양식의 필수 자료가 부족합니다: " + ", ".join(sorted(missing)))
    blocks = []
    if template["demo"]:
        blocks.append({"type": "notice", "text": "테스트용 합성 양식입니다. 실제 은행 공식 서식이 아닙니다."})
    for section in template["sections"]:
        kind = section["type"]
        if kind in ("heading", "paragraph"):
            blocks.append({"type": kind, "text": section["text"], **({"level": section["level"]} if kind == "heading" else {})})
        else:
            value = values[section["binding"]]
            if isinstance(value, str):
                blocks.append({"type": "field", "label": section["label"], "value": value})
            else:
                if section["label"]:
                    blocks.append({"type": "heading", "level": 3, "text": section["label"]})
                blocks.append(value)
    blocks.append(paragraph(f"사용 양식: {template['name']} · 버전 {template['version']}"))
    if "report_id" not in template["bindings"]:
        blocks.append({"type": "field", "label": "리포트 번호", "value": values["report_id"]})
    return {"schema_version": 1, "title": template["name"], "base_date": values["base_date"],
            "report_id": values["report_id"], "template": {k: template[k] for k in ("id", "name", "version", "hash")},
            "blocks": blocks}


def document_text(document):
    """채팅의 문자열 필드용 일반 텍스트. 원본/화면/출력의 데이터 형식은 JSON이다."""
    lines = []
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "heading":
            lines.append(block["text"])
        elif kind in ("paragraph", "notice"):
            lines.append(block["text"])
        elif kind == "field":
            lines.append((block["label"] + ": " if block["label"] else "") + block["value"])
        elif kind == "list":
            lines.append("\n".join("• " + item for item in block["items"]))
        elif kind == "table":
            lines.append("\n".join([" / ".join(map(str, block["columns"]))] +
                                   [" / ".join(map(str, row)) for row in block["rows"]]))
    return "\n\n".join(lines)
