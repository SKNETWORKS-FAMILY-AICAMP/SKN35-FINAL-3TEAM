# -*- coding: utf-8 -*-
"""그림 속 글자 꺼내기 단위 테스트."""
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    PictureItem,
    ProvenanceItem,
    Size,
    TextItem,
)

from knowledge.ingestion.parsers.pdf.picture_text import lift_picture_text

W, H = 600, 800


def _prov(l, t, r, b, page=1):
    return ProvenanceItem(page_no=page, charspan=(0, 0),
                          bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT))


def _doc_with_picture(box, lines):
    """그림 하나 + 그림 자식 글자 줄들. lines: [(글자, l, t, r, b)]."""
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=W, height=H))
    d.add_text(label=DocItemLabel.TEXT, text="그림 앞 문단", prov=_prov(50, 100, 500, 120))
    pic = d.add_picture(prov=_prov(*box))
    for txt, l, t, r, b in lines:
        d.add_text(label=DocItemLabel.TEXT, text=txt, prov=_prov(l, t, r, b), parent=pic)
    d.add_text(label=DocItemLabel.TEXT, text="그림 뒤 문단", prov=_prov(50, 600, 500, 620))
    return d


FLOW = [("공동주택 관리", 100, 300, 200, 312), ("실태조사 과정에서", 100, 314, 220, 326),
        ("서울시 송부", 300, 400, 380, 412)]


def _body_texts(d):
    return [it.text for it, _ in d.iterate_items() if isinstance(it, TextItem)]


def test_picture_text_lifted_after_picture_in_order():
    """그림 자식 글자가 그림 바로 뒤 본문 글자 하나로 나오고, 가까운 줄은 이어진다."""
    d = _doc_with_picture((80, 280, 520, 500), FLOW)
    log = lift_picture_text(d)
    assert _body_texts(d) == ["그림 앞 문단", "공동주택 관리 실태조사 과정에서\n서울시 송부", "그림 뒤 문단"]
    pic = next(p for p, _ in d.iterate_items() if isinstance(p, PictureItem))
    assert pic.children == []                               # 두 번 나오지 않게 그림에서는 지움
    assert log.pictures[0]["lines"] == 3


def test_small_logo_in_margin_skipped():
    """쪽 위 여백의 작은 그림(로고)의 깨진 글자는 옮기지 않는다."""
    d = _doc_with_picture((60, 90, 110, 120), [("|Y롱lY", 62, 92, 100, 100), ("홈페이지", 62, 104, 100, 112)])
    lift_picture_text(d)
    assert "|Y롱lY" not in "".join(_body_texts(d))


def test_ocr_only_picture_text_skipped_when_words_given():
    """글자층 낱말이 없는 그림(래스터 그림 OCR 결과)은 words를 주면 옮기지 않는다."""
    d = _doc_with_picture((80, 280, 520, 500), [("쿠분", 100, 300, 130, 312), ("활목을 평가통급", 100, 320, 200, 332)])
    lift_picture_text(d, words={1: []})
    assert _body_texts(d) == ["그림 앞 문단", "그림 뒤 문단"]


def test_text_layer_picture_lifted_when_words_match():
    words = {1: [("공동주택", 100, 300, 150, 312), ("관리", 152, 300, 200, 312), ("실태조사", 100, 314, 150, 326),
                 ("과정에서", 152, 314, 220, 326), ("서울시", 300, 400, 340, 412), ("송부", 342, 400, 380, 412)]}
    d = _doc_with_picture((80, 280, 520, 500), FLOW)
    lift_picture_text(d, words=words)
    assert len(_body_texts(d)) == 3


def test_picture_lines_overlapping_table_not_lifted():
    """그림 자식 글자 중 본문 표 자리와 겹치는 줄은 이미 본문에 있는 글자라 옮기지 않는다."""
    from docling_core.types.doc import TableCell, TableData

    d = _doc_with_picture((80, 280, 520, 500), FLOW)
    cell = TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=1,
                     row_span=1, col_span=1, text="공동주택 관리")
    d.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=[cell]), prov=_prov(95, 295, 230, 330))
    lift_picture_text(d)
    lifted = [t for t in _body_texts(d) if "서울시 송부" in t]
    assert lifted and "공동주택 관리" not in lifted[0]
