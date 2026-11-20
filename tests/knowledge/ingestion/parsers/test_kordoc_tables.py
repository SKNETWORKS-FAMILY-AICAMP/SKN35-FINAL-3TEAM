# -*- coding: utf-8 -*-
"""혼합 방식(kordoc 표 고르기) 단위 테스트 — 가짜 kordoc JSON."""
import json

from docling_core.types.doc import (BoundingBox, CoordOrigin, DoclingDocument, ProvenanceItem, Size, TableCell,
                                    TableData, TableItem)

from knowledge.ingestion.parsers.pdf.kordoc_tables import apply_kordoc_tables, kordoc_table_data, load_kordoc_tables

H = 800


def _doc(texts=("구분", "값")):
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=600, height=H))
    cells = [TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=i, end_col_offset_idx=i + 1,
                       row_span=1, col_span=1, text=t) for i, t in enumerate(texts)]
    prov = ProvenanceItem(page_no=1, charspan=(0, 0), bbox=BoundingBox(l=100, t=100, r=400, b=200, coord_origin=CoordOrigin.TOPLEFT))
    d.add_table(data=TableData(num_rows=1, num_cols=2, table_cells=cells), prov=prov)
    return d


def _kordoc(tmp_path, cells, bbox=True):
    blk = {"type": "table", "pageNumber": 1, "table": {"rows": len(cells), "cols": len(cells[0]), "cells": cells}}
    if bbox:
        blk["bbox"] = {"page": 1, "x": 100, "y": H - 200, "width": 300, "height": 100}   # 왼쪽 아래 원점, y = 아래 끝
    p = tmp_path / "k.json"
    p.write_text(json.dumps({"blocks": [blk]}, ensure_ascii=False), encoding="utf-8")
    return p


GRID = [[{"text": "구분", "rowSpan": 2, "colSpan": 1}, {"text": "값", "rowSpan": 1, "colSpan": 1}],
        [{"text": "", "rowSpan": 1, "colSpan": 1}, {"text": "10", "rowSpan": 1, "colSpan": 1}]]
WORDS = {1: [("구분", 110, 110, 140, 122), ("값", 210, 110, 220, 122), ("10", 210, 150, 225, 162)]}


def test_kordoc_grid_skips_covered_positions():
    data = kordoc_table_data({"cells": GRID})
    assert (data.num_rows, data.num_cols, len(data.table_cells)) == (2, 2, 3)


def test_docling_table_replaced_by_kordoc_when_text_kept(tmp_path):
    d = _doc()
    kt = load_kordoc_tables(_kordoc(tmp_path, GRID), d)
    log = apply_kordoc_tables(d, kt, WORDS, protected=set())
    t = next(it for it, _ in d.iterate_items() if isinstance(it, TableItem))
    assert log.replaced and (t.data.num_rows, t.data.num_cols) == (2, 2)


def test_protected_line_grid_table_kept(tmp_path):
    """선 격자 표는 kordoc 행이 더 많지 않으면 그대로 둔다."""
    grid1 = [[{"text": "구분", "rowSpan": 1, "colSpan": 1}, {"text": "값", "rowSpan": 1, "colSpan": 1}]]
    d = _doc()
    t = next(it for it, _ in d.iterate_items() if isinstance(it, TableItem))
    log = apply_kordoc_tables(d, load_kordoc_tables(_kordoc(tmp_path, grid1), d), WORDS, protected={t.self_ref})
    assert not log.replaced and "선 격자" in log.kept[0]["why"]


def test_kordoc_missing_text_kept_ours(tmp_path):
    """kordoc 칸 글자가 비어 있으면(글자 놓침) 우리 표를 둔다."""
    empty = [[{"text": "", "rowSpan": 1, "colSpan": 1}, {"text": "", "rowSpan": 1, "colSpan": 1}]]
    d = _doc()
    log = apply_kordoc_tables(d, load_kordoc_tables(_kordoc(tmp_path, empty), d), WORDS, protected=set())
    assert not log.replaced and "글자 보존" in log.kept[0]["why"]


def test_kordoc_extra_text_kept_ours(tmp_path):
    """kordoc 칸에 그 자리에 없는 글자가 많으면(쪽 밖 글자 등) 우리 표를 둔다."""
    noisy = [[{"text": "구분 가나다라마바사아자차카타파하", "rowSpan": 1, "colSpan": 1}, {"text": "값 10", "rowSpan": 1, "colSpan": 1}]]
    d = _doc()
    log = apply_kordoc_tables(d, load_kordoc_tables(_kordoc(tmp_path, noisy), d), WORDS, protected=set())
    assert not log.replaced and "밖 글자" in log.kept[0]["why"]


def test_multipage_kordoc_table_absorbs_neighbor_page_table(tmp_path):
    """kordoc이 두 쪽 표를 하나로 합쳐 냈으면, 이웃 쪽 우리 표까지 함께 바꾸고 그 표는 지운다(글자 중복 방지)."""
    d = DoclingDocument(name="t")
    for pg in (1, 2):
        d.add_page(page_no=pg, size=Size(width=600, height=H))
    for pg, texts in ((1, ("구분", "값")), (2, ("합계", "30"))):
        cells = [TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=i, end_col_offset_idx=i + 1,
                           row_span=1, col_span=1, text=t) for i, t in enumerate(texts)]
        prov = ProvenanceItem(page_no=pg, charspan=(0, 0), bbox=BoundingBox(l=100, t=100, r=400, b=200, coord_origin=CoordOrigin.TOPLEFT))
        d.add_table(data=TableData(num_rows=1, num_cols=2, table_cells=cells), prov=prov)
    grid = [[{"text": "구분", "rowSpan": 1, "colSpan": 1}, {"text": "값", "rowSpan": 1, "colSpan": 1}],
            [{"text": "합계", "rowSpan": 1, "colSpan": 1}, {"text": "30", "rowSpan": 1, "colSpan": 1}]]
    words = {1: [("구분", 110, 110, 140, 122), ("값", 210, 110, 220, 122)], 2: [("합계", 110, 110, 140, 122), ("30", 210, 110, 225, 122)]}
    log = apply_kordoc_tables(d, load_kordoc_tables(_kordoc(tmp_path, grid), d), words, protected=set())
    ts = [it for it, _ in d.iterate_items() if isinstance(it, TableItem)]
    assert len(ts) == 1 and ts[0].data.num_rows == 2 and log.replaced[0]["absorbed_pages"] == [2]


def test_multipage_clip_table_absorbs_paragraph_on_next_page():
    """여러 쪽 클립 표(pages 를 아는 후보)의 뒤 쪽 조각을 우리가 글 문단으로 읽었으면, 그 문단까지 흡수해 바꾸고 문단은 지운다.
    표 열 범위 밖 문단과 짧은 문단(쪽 번호)은 그대로 둔다."""
    from docling_core.types.doc import DocItemLabel, TextItem
    d = DoclingDocument(name="t")
    for pg in (1, 2):
        d.add_page(page_no=pg, size=Size(width=600, height=H))
    cells = [TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=i, end_col_offset_idx=i + 1,
                       row_span=1, col_span=1, text=t) for i, t in enumerate(("현행", "개정안"))]
    box = BoundingBox(l=100, t=100, r=400, b=200, coord_origin=CoordOrigin.TOPLEFT)
    d.add_table(data=TableData(num_rows=1, num_cols=2, table_cells=cells), prov=ProvenanceItem(page_no=1, charspan=(0, 0), bbox=box))

    def para(text, l, r):
        b = BoundingBox(l=l, t=60, r=r, b=80, coord_origin=CoordOrigin.TOPLEFT)
        d.add_text(label=DocItemLabel.TEXT, text=text, prov=ProvenanceItem(page_no=2, charspan=(0, len(text)), bbox=b))
    para("제5조 위험관리 기준을 정한다", 110, 380)         # 표 조각 (흡수)
    para("제5조 위험관리 기준을 정한다", 450, 590)         # 표 열 범위 밖 (둠)
    para("- 3 -", 240, 260)                                # 짧은 문단 (둠)
    grid = [[{"text": "현행"}, {"text": "개정안"}], [{"text": "제5조 위험관리"}, {"text": "기준을 정한다"}]]
    kt = [{"page": 1, "box": (100, 100, 400, 200), "data": kordoc_table_data({"cells": grid}), "pages": [1, 2]}]
    words = {1: [("현행", 110, 110, 140, 122), ("개정안", 210, 110, 250, 122)]}
    log = apply_kordoc_tables(d, kt, words, protected=set())
    texts = [it.text for it, _ in d.iterate_items() if isinstance(it, TextItem)]
    assert log.replaced and log.replaced[0]["absorbed_text"] == 1
    assert texts == ["제5조 위험관리 기준을 정한다", "- 3 -"]


def test_multipage_candidate_never_absorbs_earlier_page_table():
    """여러 쪽 후보 표는 자기가 걸친 뒤 쪽 조각만 흡수한다 — 앞 쪽의 다른 표는 글자가 모두 후보 안에 있어도 지우지 않는다(예전 ±3쪽 규칙은 지웠다)."""
    d = DoclingDocument(name="t")
    for pg in (1, 2, 3):
        d.add_page(page_no=pg, size=Size(width=600, height=H))
    for pg, texts in ((1, ("현행", "제2조")), (2, ("현행", "개정안")), (3, ("제1조", "제2조"))):
        cells = [TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=i, end_col_offset_idx=i + 1,
                           row_span=1, col_span=1, text=t) for i, t in enumerate(texts)]
        prov = ProvenanceItem(page_no=pg, charspan=(0, 0), bbox=BoundingBox(l=100, t=100, r=400, b=200, coord_origin=CoordOrigin.TOPLEFT))
        d.add_table(data=TableData(num_rows=1, num_cols=2, table_cells=cells), prov=prov)
    grid = [[{"text": "현행"}, {"text": "개정안"}], [{"text": "제1조"}, {"text": "제2조"}]]
    kt = [{"page": 2, "box": (100, 100, 400, 200), "data": kordoc_table_data({"cells": grid}), "pages": [2, 3]}]
    words = {2: [("현행", 110, 110, 140, 122), ("개정안", 210, 110, 250, 122)]}
    log = apply_kordoc_tables(d, kt, words, protected=set())
    pages = sorted(it.prov[0].page_no for it, _ in d.iterate_items() if isinstance(it, TableItem))
    assert log.replaced and pages == [1, 2] and log.replaced[0]["absorbed_pages"] == [3]


def test_trusted_clip_candidate_overrides_protected_table(tmp_path):
    """칸 클립 후보(trusted)는 선 격자·규칙선 보호 표도 바꾼다 (글자 보존 안전장치는 그대로). 보통 후보는 보호를 따른다."""
    d = _doc()
    t = next(it for it, _ in d.iterate_items() if isinstance(it, TableItem))
    kt = load_kordoc_tables(_kordoc(tmp_path, GRID), d)
    assert not apply_kordoc_tables(d, kt, WORDS, protected={t.self_ref}).replaced
    d2 = _doc()
    t2 = next(it for it, _ in d2.iterate_items() if isinstance(it, TableItem))
    log = apply_kordoc_tables(d2, load_kordoc_tables(_kordoc(tmp_path, GRID), d2), WORDS, protected={t2.self_ref},
                              always_keep={t2.self_ref}, trusted=True)
    assert log.replaced and t2.data.num_rows == 2
