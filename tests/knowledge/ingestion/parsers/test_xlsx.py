# -*- coding: utf-8 -*-
"""XLSX 후처리(parsers/office/xlsx.py) — 시트 이름 제목, 1칸 표 → 글, 숫자 서식 표시값, 결과값 없는 수식.
엑셀 파일은 openpyxl 로 그 자리에서 만든다 (내용은 지어낸 값)."""
import datetime as dt
import io

import pytest
from openpyxl import Workbook

from knowledge.ingestion.parsers.office.xlsx import display_number
from knowledge.ingestion.parsers.pipeline import DocParser


def _bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def parser():
    return DocParser()


@pytest.mark.parametrize("value, fmt, want", [
    (1250000, "#,##0", "1,250,000"),
    (35000000, '#,##0"원"', "35,000,000원"),
    (-1200000, "#,##0;(#,##0)", "(1,200,000)"),
    (-1200000, "#,##0", "-1,200,000"),
    (1234.5, "#,##0.00", "1,234.50"),
    (1234.5, "0.0#", "1234.5"),
    (1500000, "#,##0,", "1,500"),                    # 끝 쉼표 = 천 단위
    (5000, "[$₩-412]#,##0", "₩5,000"),
    (5000, "#,##0_);[Red](#,##0)", "5,000"),
    (0.045, "0.00%", None),                          # % 는 Docling 이 이미 처리 → 손대지 않음
    (46000, "yyyy-mm-dd", None),                     # 날짜 서식
    (12, "General", None),
])
def test_display_number(value, fmt, want):
    assert display_number(value, fmt) == want


def test_sheet_names_single_cells_and_formats(parser):
    wb = Workbook()
    ws = wb.active; ws.title = "심사요약"
    ws["A1"] = "운전자금 심사 요약"
    ws.append([]); ws.append(["항목", "금액"])
    ws.append(["신청금액", 500000000]); ws["B4"].number_format = '#,##0"원"'
    ws.append(["만기일", dt.date(2026, 1, 15)]); ws["B5"].number_format = "yyyy-mm-dd"
    ws.append([]); ws.append(["※ 금액 단위: 원"])
    hidden = wb.create_sheet("작업용"); hidden.append(["임시", 1]); hidden.sheet_state = "hidden"
    r = parser.parse(_bytes(wb), file_name="요약.xlsx")
    assert r.status == "ok", r.reason
    md = r.markdown
    assert md.startswith("## 심사요약")                          # 시트 이름 제목
    assert "### 운전자금 심사 요약" in md                         # 첫 표 앞 1칸 → 제목
    assert "※ 금액 단위: 원" in md and "| ※" not in md           # 첫 표 뒤 1칸 → 문단
    assert "500,000,000원" in md and "2026-01-15" in md            # 서식 표시값, 날짜는 그대로
    assert "작업용" not in md                                      # 숨긴 시트는 본문에 안 나온다
    table = next(e for e in r.elements if e["type"] == "table")
    assert table["heading_path"] == ["심사요약", "운전자금 심사 요약"]
    assert r.log["xlsx"]["single_cell_to_heading"] == 1 and r.log["xlsx"]["single_cell_to_text"] == 1


def test_formula_without_cache_is_logged(parser):
    wb = Workbook()
    ws = wb.active; ws.title = "합계"
    ws.append(["분기", "신규"]); ws.append(["1분기", 120]); ws.append(["2분기", 150])
    ws.append(["합계", "=SUM(B2:B3)"])                    # openpyxl 은 결과값(캐시)을 저장하지 않는다
    r = parser.parse(_bytes(wb), file_name="합계.xlsx")
    assert r.log["xlsx"]["formula_no_cache"] == ["합계!B4"]
    assert "=SUM(B2:B3)" in r.markdown
