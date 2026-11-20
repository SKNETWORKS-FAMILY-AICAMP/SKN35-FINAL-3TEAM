# -*- coding: utf-8 -*-
"""쪽 넘김 표 합치기 단위 테스트 (가짜 DoclingDocument로 위치·칸을 직접 만든다)."""
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
    TableItem,
)

from knowledge.ingestion.parsers.pdf.table_merge import merge_split_tables

W, H = 600, 800
# 열 x 범위 (글자가 놓인 자리)
COLS = [(60, 100), (120, 300), (320, 500)]


def _prov(page, t, b, l=50, r=550):
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT),
                          charspan=(0, 0))


def _table(rows, cols=COLS):
    """rows: [[칸 글자...], ...] → TableData (칸 bbox는 COLS의 x 범위)."""
    cells = []
    for r, row in enumerate(rows):
        for c, txt in enumerate(row):
            x0, x1 = cols[c]
            cells.append(TableCell(start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                                   end_col_offset_idx=c + 1, row_span=1, col_span=1, text=txt,
                                   bbox=BoundingBox(l=x0, t=100 + r * 20, r=x1, b=115 + r * 20,
                                                    coord_origin=CoordOrigin.TOPLEFT)))
    return TableData(num_rows=len(rows), num_cols=len(cols), table_cells=cells)


def _doc(pages=3):
    d = DoclingDocument(name="t")
    for p in range(1, pages + 1):
        d.add_page(page_no=p, size=Size(width=W, height=H))
    return d


def _tables(d):
    return [it for it, _ in d.iterate_items() if isinstance(it, TableItem)]


def _texts(t):
    return [[c.text for c in row] for row in t.data.grid]


HEAD = ["번호", "항목", "결과"]


def test_split_table_merged_and_repeated_header_dropped():
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"], ["2", "나", "양호"]]), prov=_prov(1, 500, 760))
    d.add_table(data=_table([HEAD, ["3", "다", "미흡"], ["4", "라", "양호"]]), prov=_prov(2, 60, 200))
    log = merge_split_tables(d)
    (t,) = _tables(d)
    assert t.data.num_rows == 5 and t.data.num_cols == 3
    assert _texts(t)[3] == ["3", "다", "미흡"]
    assert log.merged[0]["header_rows_dropped"] == 1
    assert [p.page_no for p in t.prov] == [1, 2]


def test_three_pages_chain():
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"]]), prov=_prov(1, 600, 760))
    d.add_table(data=_table([HEAD, ["2", "나", "양호"]]), prov=_prov(2, 60, 760))
    d.add_table(data=_table([HEAD, ["3", "다", "양호"]]), prov=_prov(3, 60, 200))
    merge_split_tables(d)
    (t,) = _tables(d)
    assert [r[0] for r in _texts(t)] == ["번호", "1", "2", "3"]


def test_text_between_blocks_merge():
    """앞 표 아래에 문단이 있으면 표가 끝난 것이다."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"]]), prov=_prov(1, 500, 700))
    d.add_text(label=DocItemLabel.TEXT, text="표 아래 문단", prov=_prov(1, 710, 760))
    d.add_table(data=_table([HEAD, ["2", "나", "양호"]]), prov=_prov(2, 60, 200))
    merge_split_tables(d)
    assert len(_tables(d)) == 2


def test_misordered_paragraph_above_table_does_not_block():
    """읽기 순서만 표 뒤로 밀린, 실제로는 앞 표 위에 있는 문단은 무시한다 (S2 Word PDF 사례)."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"]]), prov=_prov(1, 600, 760))
    d.add_text(label=DocItemLabel.TEXT, text="표 위 문단", prov=_prov(1, 300, 400))
    d.add_table(data=_table([HEAD, ["2", "나", "양호"]]), prov=_prov(2, 60, 200))
    merge_split_tables(d)
    assert len(_tables(d)) == 1


def test_next_table_mid_page_is_new_table():
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"]]), prov=_prov(1, 600, 760))
    d.add_table(data=_table([HEAD, ["2", "나", "양호"]]), prov=_prov(2, 400, 600))
    merge_split_tables(d)
    assert len(_tables(d)) == 2


def test_not_consecutive_pages():
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"]]), prov=_prov(1, 600, 760))
    d.add_table(data=_table([HEAD, ["2", "나", "양호"]]), prov=_prov(3, 60, 200))
    merge_split_tables(d)
    assert len(_tables(d)) == 2


def test_row_cut_by_page_is_joined():
    """번호 열이 비어 있는 첫 행 = 앞 표 마지막 행이 쪽에서 잘린 것 → 글자를 이어 붙인다."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"], ["2", "나", "양호"], ["3", "개인정보 암호화 저", "미"]]),
                prov=_prov(1, 500, 760))
    d.add_table(data=_table([["장", "흡"]], cols=COLS[1:]), prov=_prov(2, 60, 90))
    log = merge_split_tables(d)
    (t,) = _tables(d)
    assert t.data.num_rows == 4
    assert _texts(t)[3] == ["3", "개인정보 암호화 저 장", "미 흡"]
    assert log.merged[0]["row_continued"] is True


def test_fewer_columns_mapped_by_position():
    """빈 열이 사라져 열 수가 줄어든 뒤 표는 x 위치로 열을 맞춘다."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"]]), prov=_prov(1, 600, 760))
    d.add_table(data=_table([["2", "나"]], cols=[COLS[0], COLS[1]]), prov=_prov(2, 60, 90))
    merge_split_tables(d)
    (t,) = _tables(d)
    assert t.data.num_cols == 3
    assert _texts(t)[2] == ["2", "나", ""]


def test_unmappable_columns_refused():
    """두 열이 한 열에 겹치면 합치지 않는다."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"]]), prov=_prov(1, 600, 760))
    d.add_table(data=_table([["2", "나"]], cols=[(130, 200), (210, 290)]), prov=_prov(2, 60, 90))
    log = merge_split_tables(d)
    assert len(_tables(d)) == 2
    assert log.refused and log.refused[0]["why"] == "열을 맞출 수 없음"


# ---------------------------------------------------------------- 2차: 번호 열 완화 · 세로 병합 잇기 · 선으로 열 맞추기
from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines  # noqa: E402


def _cell(r, c, txt, rs=1, cs=1, cols=COLS):
    x0, x1 = cols[c][0], cols[c + cs - 1][1]
    return TableCell(start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                     end_col_offset_idx=c + cs, row_span=rs, col_span=cs, text=txt,
                     bbox=BoundingBox(l=x0, t=100 + r * 20, r=x1, b=115 + r * 20, coord_origin=CoordOrigin.TOPLEFT))


def test_row_cut_found_when_subheader_row_has_empty_id():
    """2줄 머리글의 아래 줄은 번호 칸이 비어 있다 → 그래도 번호 열로 인정해 잘린 행을 이어 붙인다 (스캔 S2)."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["", "세부", "기한"], ["5", "가", "10."], ["12", "나", "10."], ["13", "다", "10."], ["27", "개인정보 암호화 저", "11."]]),
                prov=_prov(1, 500, 760))
    d.add_table(data=_table([["장", "30."]], cols=COLS[1:]), prov=_prov(2, 60, 90))
    log = merge_split_tables(d)
    (t,) = _tables(d)
    assert t.data.num_rows == 6
    assert _texts(t)[5] == ["27", "개인정보 암호화 저 장", "11. 30."]
    assert log.merged[0]["row_continued"] is True


def test_vertical_span_continues_on_next_page():
    """앞 표 마지막 행까지 내려온 세로 병합 칸(영역) 아래, 뒤 표 위쪽 빈 칸은 그 병합의 연속이다."""
    d = _doc()
    a = TableData(num_rows=3, num_cols=3, table_cells=[
        _cell(0, 0, "영역"), _cell(0, 1, "번호"), _cell(0, 2, "항목"),
        _cell(1, 0, "물리적 보안", rs=2), _cell(1, 1, "19"), _cell(1, 2, "출입 통제"),
        _cell(2, 1, "20"), _cell(2, 2, "CCTV")])
    b = TableData(num_rows=3, num_cols=3, table_cells=[
        _cell(0, 1, "21"), _cell(0, 2, "문서고"),
        _cell(1, 1, "22"), _cell(1, 2, "반출 통제"),
        _cell(2, 0, "개인정보"), _cell(2, 1, "23"), _cell(2, 2, "방침")])
    d.add_table(data=a, prov=_prov(1, 600, 760))
    d.add_table(data=b, prov=_prov(2, 60, 200))
    log = merge_split_tables(d)
    (t,) = _tables(d)
    host = next(c for c in t.data.table_cells if c.text == "물리적 보안")
    assert (host.start_row_offset_idx, host.row_span) == (1, 4)
    assert t.data.grid[5][0].text == "개인정보"
    assert log.merged[0]["spans_extended"] == [0]


def test_empty_cell_under_single_row_cell_is_not_span():
    """한 행짜리 칸 아래 빈 칸은 그냥 빈 칸이다 (예: 미흡 열이 비어 있음)."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"], ["2", "나", "양호"]]), prov=_prov(1, 600, 760))
    b = TableData(num_rows=1, num_cols=3, table_cells=[_cell(0, 0, "3"), _cell(0, 1, "다")])
    d.add_table(data=b, prov=_prov(2, 60, 90))
    log = merge_split_tables(d)
    (t,) = _tables(d)
    assert all(c.row_span == 1 for c in t.data.table_cells)
    assert log.merged[0]["spans_extended"] == []


def _lines(xs, top, bottom, pages=(1, 2)):
    return {p: PageLines(height=H, v=[(x, top, bottom) for x in xs]) for p in pages}


def test_header_with_merged_column_mapped_by_lines():
    """쪽 끝에 머리글 1행만 남고, 그 머리글은 두 열(번호·항목)을 한 칸으로 읽힌 경우 (HWP S2).
    글자 위치로는 못 맞추지만 세로선으로 '그 열 안에 있음'을 알 수 있다."""
    cols6 = [(78, 85), (93, 220), (260, 280), (294, 310), (374, 400), (449, 470)]
    d = _doc()
    hdr = TableData(num_rows=1, num_cols=4, table_cells=[
        _cell(0, 0, "영역", cols=[(78, 115), (127, 176), (260, 282), (374, 396)]),
        _cell(0, 1, "점검 항목", cols=[(78, 115), (127, 176), (260, 282), (374, 396)]),
        _cell(0, 2, "결과", cols=[(78, 115), (127, 176), (260, 282), (374, 396)]),
        _cell(0, 3, "조치", cols=[(78, 115), (127, 176), (260, 282), (374, 396)])])
    body = TableData(num_rows=1, num_cols=6, table_cells=[_cell(0, c, t, cols=cols6) for c, t in
                                                          enumerate(["관리", "1 계획", "○", "", "-", "-"]) if t])
    d.add_table(data=hdr, prov=_prov(1, 700, 760, l=71, r=500))
    d.add_table(data=body, prov=_prov(2, 80, 120, l=71, r=500))
    lines = {1: PageLines(height=H, v=[(x, 700, 760) for x in (72, 87, 121, 254, 368, 499)]),
             2: PageLines(height=H, v=[(x, 80, 120) for x in (72, 87, 121, 254, 288, 368, 443, 499)])}
    assert len(_tables(d)) == 2
    log = merge_split_tables(d, lines)
    (t,) = _tables(d)
    assert t.data.num_cols == 6
    # 앞 쪽에는 288·443 선이 없다 → '결과'는 양호·미흡, '조치'는 기한·담당 두 열에 걸친 칸
    assert [c.text for c in t.data.grid[0]] == ["영역", "점검 항목", "결과", "결과", "조치", "조치"]
    spans = {(c.start_col_offset_idx, c.col_span) for c in t.data.table_cells if c.start_row_offset_idx == 0}
    assert spans == {(0, 1), (1, 1), (2, 2), (4, 2)}
    assert log.merged[0]["col_map"] == "선"


def test_form_column_without_single_cells_mapped_by_lines():
    """서식표: 어떤 열에는 병합 칸만 있어 글자 위치를 모른다 (S1 Word). 선으로는 맞출 수 있다."""
    c4 = [(91, 111), (133, 190), (201, 228), (240, 327)]
    d = _doc()
    a = TableData(num_rows=2, num_cols=3, table_cells=[
        _cell(0, 0, "보고 일시", cols=c4), _cell(0, 2, "보고자", cols=c4),
        _cell(1, 0, "사고 유형", cols=c4), _cell(1, 1, "☐ 유출 ☐ 기타", cs=2, cols=c4)])
    b = TableData(num_rows=1, num_cols=4, table_cells=[_cell(0, c, t, cols=c4) for c, t in
                                                        enumerate(["연락처", "010", "전자우편", "a@b"])])
    d.add_table(data=a, prov=_prov(1, 600, 760, l=84, r=333))
    d.add_table(data=b, prov=_prov(2, 60, 100, l=84, r=333))
    log = merge_split_tables(d, _lines((85, 127, 195, 234, 333), 0, H))
    (t,) = _tables(d)
    assert (t.data.num_rows, t.data.num_cols) == (3, 4)
    assert [c.text for c in t.data.grid[2]] == ["연락처", "010", "전자우편", "a@b"]
    assert log.merged[0]["col_map"] == "선"


def test_wide_table_with_split_column_refused():
    """넓은 표의 두 열이 같은 세로선에서 시작하면 표 모델이 한 열을 쪼갠 것 → 합치지 않는다 (붙임4)."""
    d = _doc()
    a = TableData(num_rows=2, num_cols=3, table_cells=[
        _cell(0, 0, "구분", cols=[(58, 80), (365, 378), (392, 404)]),
        _cell(0, 1, "지", cols=[(58, 80), (365, 378), (392, 404)]),
        _cell(0, 2, "적", cols=[(58, 80), (365, 378), (392, 404)]),
        _cell(1, 0, "1", cols=[(58, 80), (365, 378), (392, 404)])])
    b = TableData(num_rows=1, num_cols=2, table_cells=[_cell(0, 0, "2", cols=[(58, 80), (330, 400)]),
                                                       _cell(0, 1, "적정", cols=[(58, 80), (330, 400)])])
    d.add_table(data=a, prov=_prov(1, 600, 760, l=52, r=547))
    d.add_table(data=b, prov=_prov(2, 60, 100, l=52, r=547))
    log = merge_split_tables(d, _lines((52, 315, 546), 0, H))
    assert len(_tables(d)) == 2
    assert log.refused[0]["why"] == "열을 맞출 수 없음"


def test_separate_tables_with_different_headers_not_merged():
    """쪽 끝·쪽 머리에 연달아 있어도 머리글이 서로 다른 두 표는 별개 표다."""
    d = _doc()
    d.add_table(data=_table([HEAD, ["1", "가", "양호"], ["2", "나", "양호"]]), prov=_prov(1, 500, 760))
    d.add_table(data=_table([["구분", "내용", "비고"], ["3", "다", "미흡"]]), prov=_prov(2, 60, 200))
    log = merge_split_tables(d)
    assert len(_tables(d)) == 2
    assert log.refused[0]["why"] == "머리글이 다른 새 표"
