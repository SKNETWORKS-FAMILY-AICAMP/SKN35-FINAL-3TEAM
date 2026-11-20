# -*- coding: utf-8 -*-
"""이미지 쪽 다시 읽기 단위 테스트 (가짜 OCR 엔진 — 판단 규칙만 본다)."""
from types import SimpleNamespace

from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
    TextItem,
)

from knowledge.ingestion.parsers.pdf.ocr_reread import read_lines, reread_page

W, H = 600, 800


def _prov(l, t, r, b):
    return ProvenanceItem(page_no=1, charspan=(0, 0),
                          bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT).to_bottom_left_origin(H))


def _doc():
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=W, height=H))
    return d


def _texts(d):
    return [it.text for it, _ in d.iterate_items() if isinstance(it, TextItem)]


class FakeEngine:
    """배율마다 미리 정한 결과를 돌려준다. 그림 크기(가로 픽셀)로 배율을 구분한다."""

    def __init__(self, by_width):
        self.by_width = by_width

    def __call__(self, arr):
        res = self.by_width[arr.shape[1]]
        return SimpleNamespace(boxes=[r[0] for r in res], txts=[r[1] for r in res], scores=[r[2] for r in res])


def _quad(l, t, r, b):
    return [[l, t], [r, t], [r, b], [l, b]]


def test_read_lines_keeps_confident_and_best_per_place():
    """신뢰도 낮은 줄은 버리고, 배율끼리 같은 자리면 신뢰도 높은 쪽을 남긴다. 좌표는 쪽 좌표로 바뀐다."""
    from PIL import Image

    small, big = Image.new("RGB", (100, 100)), Image.new("RGB", (200, 200))
    eng = FakeEngine({
        100: [(_quad(10, 10, 60, 20), "연임은 막고 셀피", 0.92), (_quad(10, 50, 60, 60), "잡음", 0.5)],
        200: [(_quad(20, 20, 120, 40), "셀프 연임은 막고", 0.99)],
    })
    lines = read_lines(eng, [(small, 1.0), (big, 0.5)])
    assert [(f[1], round(f[0][0])) for f in lines] == [("셀프 연임은 막고", 10)]


def test_item_text_replaced_by_reread_lines():
    d = _doc()
    d.add_text(label=DocItemLabel.TEXT, text="연임은 막고 셀피", prov=_prov(100, 100, 400, 130))
    log = reread_page(d, 1, [((110, 105, 390, 125), "셀프 연임은 막고", 0.99)])
    assert _texts(d) == ["셀프 연임은 막고"]
    assert log["replaced"] == 1


def test_partial_reread_keeps_original():
    """다시 읽은 글자가 원래 글자의 절반도 안 되면 원래 글자를 지킨다."""
    d = _doc()
    d.add_text(label=DocItemLabel.TEXT, text="금융회사 지배구조 개선방안을 발표하였습니다", prov=_prov(100, 100, 500, 160))
    reread_page(d, 1, [((110, 105, 200, 125), "금융회사", 0.99)])
    assert _texts(d) == ["금융회사 지배구조 개선방안을 발표하였습니다"]


def test_missed_line_added_after_item_above():
    d = _doc()
    d.add_text(label=DocItemLabel.TEXT, text="제목", prov=_prov(100, 100, 300, 130))
    d.add_text(label=DocItemLabel.TEXT, text="맨 아래 문단", prov=_prov(100, 600, 300, 630))
    log = reread_page(d, 1, [((100, 300, 400, 320), "세우겠습니다", 0.97)])
    assert _texts(d) == ["제목", "세우겠습니다", "맨 아래 문단"]
    assert log["added"] == 1


def test_skewed_line_already_in_nearby_item_not_added():
    """기울어진 쪽: 줄 상자가 항목 밖으로 삐져나와도 주변 항목에 같은 글자가 있으면 다시 넣지 않는다."""
    d = _doc()
    d.add_text(label=DocItemLabel.TEXT, text="이 규정에서 사용하는 용어의 뜻은 다음과 같다", prov=_prov(100, 200, 400, 220))
    reread_page(d, 1, [((100, 214, 420, 236), "이 규정에서 사용하는 용어의 뜻은 다음과 같다", 0.95)])
    assert _texts(d) == ["이 규정에서 사용하는 용어의 뜻은 다음과 같다"]


def test_lines_inside_table_and_picture_noise_ignored():
    d = _doc()
    cell = TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=1,
                     row_span=1, col_span=1, text="상")
    d.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=[cell]), prov=_prov(100, 100, 300, 200))
    d.add_picture(prov=_prov(350, 100, 500, 200))
    log = reread_page(d, 1, [((120, 120, 200, 140), "상 중 하", 0.99),     # 표 안
                             ((360, 120, 400, 140), "0000", 0.99)])         # 그림 안 짧은 잡음
    assert _texts(d) == []
    assert log["added"] == 0


def test_lines_of_different_size_keep_reading_order():
    """글자 크기가 다른 두 줄도 위→아래 순서로 잇는다 (카드뉴스 가상자산_1)."""
    d = _doc()
    d.add_text(label=DocItemLabel.TEXT, text="디지털자산법 금융회사에 준하는", prov=_prov(100, 100, 500, 160))
    reread_page(d, 1, [((110, 140, 480, 152), "금융회사에 준하는 IT 안전성", 0.95),
                       ((110, 102, 480, 130), "디지털자산법(안)을 통해", 0.95)])
    assert _texts(d) == ["디지털자산법(안)을 통해 금융회사에 준하는 IT 안전성"]


def test_longer_line_kept_over_contained_fragment():
    """배율마다 줄을 다르게 끊음: 짧은 조각의 신뢰도가 더 높아도 긴 줄을 지킨다 (카드뉴스 지배구조_2)."""
    from PIL import Image

    small, big = Image.new("RGB", (100, 100)), Image.new("RGB", (200, 200))
    eng = FakeEngine({
        100: [(_quad(10, 10, 90, 20), "기업의 장기 성장과 주주 신뢰의 출발점!", 0.93)],
        200: [(_quad(20, 20, 80, 40), "기업의 장기", 0.99)],
    })
    lines = read_lines(eng, [(small, 1.0), (big, 0.5)])
    assert [f[1] for f in lines] == ["기업의 장기 성장과 주주 신뢰의 출발점!"]
