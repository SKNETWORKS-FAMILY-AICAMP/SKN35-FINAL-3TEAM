# -*- coding: utf-8 -*-
"""가로 규칙선 표 새로 찾기(rule_tables) 단위 테스트 — 선·낱말·Docling 항목을 직접 만든다."""
from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size,
                                    TableCell, TableData, TableItem, TextItem)

from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
from knowledge.ingestion.parsers.pdf.rule_tables import apply_rule_tables, build_partial_grid

W, H = 600, 800
YS = [100, 130, 160, 190, 220]


def _form_lines():
    """체크박스 서식 모양: 양옆 세로선 없음, 가로 규칙선 50~500.
    왼쪽 '구분' 열(50~150)은 1~3행(130~220)에서 한 칸 → 그 안쪽 가로선은 150부터 시작.
    점검 열 경계 세로선 400은 1~3행에만, 150 세로선은 0~3행 전체."""
    hs = [(100, 50, 500), (130, 50, 500), (160, 150, 500), (190, 150, 500), (220, 50, 500)]
    vs = [(150, 100, 220), (400, 130, 220)]
    return PageLines(height=H, h=hs, v=vs)


def _prov(l, t, r, b):
    return ProvenanceItem(page_no=1, charspan=(0, 0), bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT))


WORDS = [("구분", 60, 108, 90, 120), ("점검사항", 200, 108, 260, 120),
         ("정책일반", 60, 165, 110, 177),
         ("법규", 160, 138, 190, 150), ("■", 420, 138, 430, 150),
         ("영향", 160, 168, 190, 180), ("□", 420, 168, 430, 180),
         ("우리말", 160, 198, 200, 210), ("■", 420, 198, 430, 210)]


def _doc(words=WORDS, n_items=None):
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=W, height=H))
    d.add_text(label=DocItemLabel.TEXT, text="표 앞 문단", prov=_prov(50, 60, 300, 72))
    for w in (words if n_items is None else words[:n_items]):
        lab = DocItemLabel.CHECKBOX_SELECTED if w[0] == "■" else DocItemLabel.TEXT
        d.add_text(label=lab, text=w[0], prov=_prov(*w[1:]))
    d.add_text(label=DocItemLabel.TEXT, text="표 뒤 문단", prov=_prov(50, 300, 300, 312))
    return d


def test_partial_grid_merges_left_column_and_keeps_header_columns():
    ys, xs, cells = build_partial_grid(_form_lines(), (50, 100, 500, 220))
    assert (len(ys) - 1, len(xs) - 1) == (4, 3)
    assert (1, 4, 0, 1) in cells                  # 왼쪽 '구분' 열: 안쪽 가로선이 150에서 시작 → 1~3행 한 칸
    assert (0, 1, 1, 3) in cells                  # 머리글 행: 400 세로선이 없음 → 가로로 합침
    assert (2, 3, 2, 3) in cells                  # 점검 칸


def test_form_without_docling_table_becomes_table(monkeypatch):
    import knowledge.ingestion.parsers.pdf.rule_tables as rt
    monkeypatch.setattr(rt, "MIN_ITEMS", 6)
    d = _doc()
    log = apply_rule_tables(d, {1: _form_lines()}, {1: WORDS})
    assert log.added and log.added[0]["size"] == "4x3"
    items = [it for it, _ in d.iterate_items()]
    assert isinstance(items[1], TableItem)                      # 표 앞 문단 바로 뒤, 원래 첫 항목 자리
    texts = [it.text for it in items if isinstance(it, TextItem)]
    assert texts == ["표 앞 문단", "표 뒤 문단"]                    # 흩어진 글자·체크박스 항목은 표로 들어가고 지워짐
    grid = [[c.text for c in row] for row in items[1].data.grid]
    assert grid[1][0] == "정책일반" and grid[1][1] == "법규" and grid[1][2] == "■"


def test_box_stack_is_not_a_table(monkeypatch):
    """띠마다 칸 하나인 상자 더미(보도자료 머리)는 표로 보지 않는다."""
    import knowledge.ingestion.parsers.pdf.rule_tables as rt
    monkeypatch.setattr(rt, "MIN_ITEMS", 6)
    pl = PageLines(height=H, h=[(y, 50, 500) for y in YS], v=[(300, 100, 130)])
    d = _doc()
    log = apply_rule_tables(d, {1: pl}, {1: WORDS})
    assert not log.added and "글 상자" in log.skipped[0]["why"]


def test_few_items_skipped():
    d = _doc(n_items=4)
    log = apply_rule_tables(d, {1: _form_lines()}, {1: WORDS})
    assert not log.added and "적음" in log.skipped[0]["why"]


def test_existing_docling_table_left_to_line_tables(monkeypatch):
    import knowledge.ingestion.parsers.pdf.rule_tables as rt
    monkeypatch.setattr(rt, "MIN_ITEMS", 6)
    d = _doc()
    cell = TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=1,
                     row_span=1, col_span=1, text="")
    d.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=[cell]), prov=_prov(50, 100, 500, 220))
    log = apply_rule_tables(d, {1: _form_lines()}, {1: WORDS})
    assert not log.added and not log.skipped


def test_bullet_items_split_into_rows_and_label_columns():
    """한 칸에 '● 이름표 : 내용' 항목 2개 → 항목마다 행, 글머리|이름표|:|내용 4칸. 옆 칸(구분)은 두 행에 걸친다."""
    from knowledge.ingestion.parsers.pdf.rule_tables import _table_data
    ys, xs = [100, 160], [50, 150, 500]
    cells = [(0, 1, 0, 1), (0, 1, 1, 2)]
    words = [("소통분야", 60, 125, 120, 137),
             ("●", 160, 105, 168, 117), ("구민", 175, 105, 200, 117), (":", 205, 105, 208, 117), ("유", 215, 105, 225, 117), ("[√]", 230, 105, 250, 117),
             ("●", 160, 135, 168, 147), ("전문가", 175, 135, 210, 147), (":", 215, 135, 218, 147), ("무", 225, 135, 235, 147)]
    data = _table_data(ys, xs, cells, words)
    assert (data.num_rows, data.num_cols) == (2, 5)
    g = [[c.text for c in row] for row in data.grid]
    assert g[0] == ["소통분야", "●", "구민", ":", "유 [√]"] and g[1] == ["소통분야", "●", "전문가", ":", "무"]


def test_single_bullet_sentence_kept_in_one_cell():
    """'◆ 문장…?' 한 항목(쌍점 없음)은 나누지 않는다 (원본도 한 칸)."""
    from knowledge.ingestion.parsers.pdf.rule_tables import _table_data
    data = _table_data([100, 130], [50, 150, 500], [(0, 1, 0, 1), (0, 1, 1, 2)],
                       [("정책일반", 60, 105, 110, 117), ("◆", 160, 105, 168, 117), ("검토하였습니까?", 175, 105, 260, 117)])
    assert (data.num_rows, data.num_cols) == (1, 2) and data.grid[0][1].text == "◆ 검토하였습니까?"


def test_inner_fragment_line_does_not_split_region():
    """같은 y 에 오른쪽 끝이 표 끝보다 안쪽인 조각선(겹줄·일부 칸만 긋는 선)이 있어도 표 자리가 끊기지 않는다
    (2026-11-27 v4: 서울 '사전 검토' 서식표가 머리행 위·가운데에서 잘리던 문제)."""
    from knowledge.ingestion.parsers.pdf.rule_tables import find_regions
    hs = [(100, 50, 500), (150, 50, 500), (150, 110, 400),        # 머리행 아래 선 + 같은 y 조각선
          (180, 110, 400), (180, 50, 500), (220, 50, 500), (260, 50, 500)]
    assert find_regions(PageLines(height=H, h=hs, v=[]), 600) == [(50, 100, 500, 260)]


def test_outer_line_with_other_right_end_still_splits():
    """표 범위 밖으로 뻗은(오른쪽 끝이 다른) 선은 예전처럼 다른 표로 나눈다."""
    from knowledge.ingestion.parsers.pdf.rule_tables import find_regions
    hs = [(100, 50, 500), (130, 50, 500), (160, 50, 500), (190, 50, 500),
          (220, 50, 560), (250, 50, 560), (280, 50, 560), (310, 50, 560)]
    assert len(find_regions(PageLines(height=H, h=hs, v=[]), 600)) == 2
