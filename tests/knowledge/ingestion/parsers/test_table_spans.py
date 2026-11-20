# -*- coding: utf-8 -*-
"""한 쪽 안 놓친 병합 칸 되살리기 단위 테스트 (가짜 DoclingDocument + 가짜 PDF 선)."""
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DoclingDocument,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
    TableItem,
)

from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
from knowledge.ingestion.parsers.pdf.table_spans import fix_table_spans

W, H = 600, 800
# 열 경계(세로선 x)와 행 경계(가로선 y). 칸 (r, c)는 X[c]~X[c+1], Y[r]~Y[r+1]
X = [100, 160, 360, 460]
Y = [100, 130, 160, 190, 220]


def _cell(r, c, txt, rs=1, cs=1, w=None):
    """칸 글자 bbox: 칸 안쪽 왼쪽 위에 붙인다. w를 주면 글자 폭(세로쓰기 조각 흉내)."""
    x0 = X[c] + 6
    x1 = x0 + (w if w is not None else 40)
    y0 = Y[r] + 8
    return TableCell(start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                     end_col_offset_idx=c + cs, row_span=rs, col_span=cs, text=txt,
                     bbox=BoundingBox(l=x0, t=y0, r=x1, b=y0 + 12, coord_origin=CoordOrigin.TOPLEFT))


def _doc(cells, nr, nc=3):
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=W, height=H))
    d.add_table(data=TableData(num_rows=nr, num_cols=nc, table_cells=cells),
                prov=ProvenanceItem(page_no=1, charspan=(0, 0),
                                    bbox=BoundingBox(l=X[0], t=Y[0], r=X[nc], b=Y[nr], coord_origin=CoordOrigin.TOPLEFT)))
    return d


def _lines(nr, nc=3, skip_h=(), skip_v=()):
    """완전한 격자 선. skip_h: 빼는 (경계 행 r, 열 c) — r행과 r+1행 사이 c열 가로선.
    skip_v: 빼는 (행 r, 경계 c) — r행에서 c열과 c+1열 사이 세로선."""
    pl = PageLines(height=H)
    for r in range(nr + 1):
        for c in range(nc):
            if (r - 1, c) not in skip_h:
                pl.h.append((Y[r], X[c], X[c + 1]))
    for c in range(nc + 1):
        for r in range(nr):
            if (r, c - 1) not in skip_v:
                pl.v.append((X[c], Y[r], Y[r + 1]))
    return {1: pl}


def _table(d):
    return next(t for t, _ in d.iterate_items() if isinstance(t, TableItem))


def _spans(d):
    return sorted((c.start_row_offset_idx, c.start_col_offset_idx, c.row_span, c.col_span)
                  for c in _table(d).data.table_cells if c.row_span > 1 or c.col_span > 1)


def test_group_column_merged_where_no_line():
    """'정 보' 묶음 칸: 1~3행 사이 0열에 가로선이 없다 → 3행짜리 칸."""
    cells = [_cell(0, 0, "대분류"), _cell(0, 1, "중분류"), _cell(0, 2, "예시"),
             _cell(1, 0, "정보"), _cell(1, 1, "개인정보"), _cell(1, 2, "명부"),
             _cell(2, 1, "영업비밀"), _cell(2, 2, "설계"),
             _cell(3, 1, "일반"), _cell(3, 2, "회의록")]
    d = _doc(cells, 4)
    log = fix_table_spans(d, _lines(4, skip_h={(1, 0), (2, 0)}))
    assert _spans(d) == [(1, 0, 3, 1)]
    assert log.tables[0]["by_lines"] == 1


def test_vertical_text_pieces_joined():
    """세로쓰기가 '대' / '분 류'로 쪼개진 칸: 둘 다 좁은 조각이면 잇고 글자도 붙인다."""
    cells = [_cell(0, 0, "대", w=10), _cell(0, 1, "중분류"), _cell(0, 2, "예시"),
             _cell(1, 0, "분 류", w=10), _cell(1, 1, "가"), _cell(1, 2, "나"),
             _cell(2, 0, "정보"), _cell(2, 1, "다"), _cell(2, 2, "라")]
    d = _doc(cells, 3)
    fix_table_spans(d, _lines(3, skip_h={(0, 0)}))
    t = _table(d)
    assert _spans(d) == [(0, 0, 2, 1)]
    assert t.data.grid[0][0].text == "대 분 류"


def test_two_long_texts_not_joined_even_without_line():
    """선이 없어도 긴 글자 칸 둘은 잇지 않는다 (음영 등으로 선이 안 잡혔을 수 있다)."""
    cells = [_cell(0, 0, "번호"), _cell(0, 1, "항목"), _cell(0, 2, "사유"),
             _cell(1, 0, "1"), _cell(1, 1, "가"), _cell(1, 2, "재심의 요청 기한 명시"),
             _cell(2, 0, "2"), _cell(2, 1, "나"), _cell(2, 2, "의장의 역할 규정")]
    d = _doc(cells, 3)
    fix_table_spans(d, _lines(3, skip_h={(1, 2)}))
    assert _spans(d) == []


def test_short_horizontal_words_not_joined():
    """짧아도 가로로 쓴 별개 칸('반원' / '반장')은 세로쓰기 조각이 아니다."""
    cells = [_cell(0, 0, "구분"), _cell(0, 1, "소속"), _cell(0, 2, "서명"),
             _cell(1, 0, "반원", w=28), _cell(1, 1, "가"), _cell(1, 2, "(인)"),
             _cell(2, 0, "반장", w=28), _cell(2, 1, "나"), _cell(2, 2, "(인)")]
    d = _doc(cells, 3)
    fix_table_spans(d, _lines(3, skip_h={(1, 0)}))
    assert _spans(d) == []


def test_column_without_any_inner_line_is_not_trusted():
    """한 열 안쪽 가로선이 하나도 안 잡히면(음영 이름표 열, S2 인쇄 PDF) 그 열은 잇지 않는다."""
    cells = [_cell(r, 0, f"이름{r}") for r in range(4)] + [_cell(r, 1, f"값{r}") for r in range(4)]
    d = _doc(cells, 4, nc=2)
    fix_table_spans(d, _lines(4, nc=2, skip_h={(0, 0), (1, 0), (2, 0)}))
    assert _spans(d) == []


def test_form_row_value_spans_columns_where_no_vertical_line():
    """서식표: 값 칸에 세로선이 없으면 여러 열에 걸친 칸 (글자 있는 칸은 하나)."""
    cells = [_cell(0, 0, "보고 일시"), _cell(0, 1, "11.17"), _cell(0, 2, "담당"),
             _cell(1, 0, "사고 유형"), _cell(1, 1, "☐ 유출 ☐ 기타")]
    d = _doc(cells, 2)
    fix_table_spans(d, _lines(2, skip_v={(1, 1)}))
    assert _spans(d) == [(1, 1, 1, 2)]


def test_table_without_lines_left_alone_except_header_rule():
    """선 없는 표(스캔): 본문은 건드리지 않고, 2줄 머리글만 구조로 고친다."""
    cells = [_cell(0, 0, "영역"), _cell(0, 1, "결과", cs=2),
             _cell(1, 1, "양호"), _cell(1, 2, "미흡"),
             _cell(2, 0, "관리"), _cell(2, 1, "○"),
             _cell(3, 1, "○")]
    d = _doc(cells, 4)
    log = fix_table_spans(d, None)
    assert _spans(d) == [(0, 0, 2, 1), (0, 1, 1, 2)]
    assert log.tables[0]["by_header"] == 1


def test_header_rule_skips_data_second_row():
    """둘째 행이 데이터 행이면(한 열짜리 머리글 아래가 대부분 차 있음) 머리글로 보지 않는다 (InDesign)."""
    cells = [_cell(0, 0, "연번"), _cell(0, 1, "구분", cs=2),
             _cell(1, 0, "1"), _cell(1, 1, "신입"), _cell(1, 2, "편입"),
             _cell(2, 0, "2"), _cell(2, 1, "○"), _cell(2, 2, "○")]
    d = _doc(cells, 3)
    fix_table_spans(d, None)
    assert _spans(d) == [(0, 1, 1, 2)]


# ---------------------------------------------------------------- 열 합치기·나누기, 줄바꿈 행, 빈 값 칸, 그림 선
def _cx(r, c, txt, x0, x1, y0=None, h=12, rs=1, cs=1):
    """글자 위치를 직접 주는 칸."""
    y0 = Y[r] + 8 if y0 is None else y0
    return TableCell(start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                     end_col_offset_idx=c + cs, row_span=rs, col_span=cs, text=txt,
                     bbox=BoundingBox(l=x0, t=y0, r=x1, b=y0 + h, coord_origin=CoordOrigin.TOPLEFT))


def _doc_box(cells, nr, nc, l=X[0], r=X[3], b=None):
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=W, height=H))
    d.add_table(data=TableData(num_rows=nr, num_cols=nc, table_cells=cells),
                prov=ProvenanceItem(page_no=1, charspan=(0, 0),
                                    bbox=BoundingBox(l=l, t=Y[0], r=r, b=b or Y[nr], coord_origin=CoordOrigin.TOPLEFT)))
    return d


def _texts(d):
    return [[c.text for c in row] for row in _table(d).data.grid]


def test_split_columns_collapsed_when_same_line():
    """표 모델이 한 열('지 원')을 글자마다 두 열로 쪼갬: 두 열이 같은 세로선에서 시작 → 한 열로 (붙임2)."""
    cells = [_cx(0, 0, "연번", 106, 130), _cx(0, 1, "기관", 166, 200), _cx(0, 2, "지", 380, 390), _cx(0, 3, "원", 410, 420),
             _cx(1, 0, "1", 106, 112), _cx(1, 1, "본청", 166, 190), _cx(1, 2, "재무과", 366, 400)]
    d = _doc_box(cells, 2, 4)
    log = fix_table_spans(d, _lines(2))
    assert _table(d).data.num_cols == 3
    assert _texts(d)[0] == ["연번", "기관", "지 원"]
    assert log.tables[0]["cols_joined"] == 1


def test_columns_not_collapsed_when_line_between_text():
    """칸 bbox가 선보다 왼쪽에서 시작해 같은 선에 맞춰져도, 두 열 글자 가운데 사이에 선이 있으면 다른 열 (신구조문)."""
    cells = [_cx(0, 0, "준칙 조항", 106, 150), _cx(0, 1, "검토의견 수정사항", 150, 350), _cx(0, 2, "사유", 366, 400),
             _cx(1, 0, "가", 106, 120), _cx(1, 1, "나", 150, 340), _cx(1, 2, "다", 366, 380)]
    d = _doc_box(cells, 2, 3)
    fix_table_spans(d, _lines(2))
    assert _table(d).data.num_cols == 3


def _form_lines(row0_only_x=None, touch=True):
    """3열 격자인데 x=360 세로선은 첫 행에만 있는 서식표 선."""
    pl = PageLines(height=H)
    for y in (Y[0], Y[1], Y[2]):
        pl.h.append((y, X[0], X[3]))
    for x in (X[0], X[1], X[3]):
        pl.v.append((x, Y[0], Y[2]))
    if row0_only_x:
        top = Y[0] + (4.5 if touch else 12)
        pl.v.append((row0_only_x, top, Y[1] - (0 if touch else 8)))
    return {1: pl}


def test_fused_column_split_by_words():
    """표 모델이 '보고자 | 테스트일' 두 칸을 한 칸으로 읽음 (S1 서식표). 첫 행에만 있는 선 + 낱말 위치로 나눈다.
    선이 없는 둘째 행 값 칸은 두 열에 걸친 칸이 된다."""
    cells = [_cx(0, 0, "보고 일시", 106, 150), _cx(0, 1, "보고자 테스트일", 166, 420),
             _cx(1, 0, "사고 유형", 106, 150), _cx(1, 1, "유출", 166, 200)]
    d = _doc_box(cells, 2, 2)
    words = {1: [("보고자", 166, 108, 200, 120), ("테스트일", 366, 108, 420, 120)]}
    log = fix_table_spans(d, _form_lines(360), words)
    assert _table(d).data.num_cols == 3
    assert _texts(d)[0] == ["보고 일시", "보고자", "테스트일"]
    assert _spans(d) == [(1, 1, 1, 2)]
    assert log.tables[0]["cols_split"] == 1


def test_lines_of_table_inside_cell_do_not_split():
    """칸 안에 그린 작은 표의 선(표 테두리에 안 닿음)으로는 열을 나누지 않는다 (신구조문 16쪽)."""
    cells = [_cx(0, 0, "보고 일시", 106, 150), _cx(0, 1, "보고자 테스트일", 166, 420),
             _cx(1, 0, "사고 유형", 106, 150), _cx(1, 1, "유출", 166, 200)]
    d = _doc_box(cells, 2, 2)
    words = {1: [("보고자", 166, 108, 200, 120), ("테스트일", 366, 108, 420, 120)]}
    fix_table_spans(d, _form_lines(360, touch=False), words)
    assert _table(d).data.num_cols == 2


def test_wrapped_row_joined():
    """칸 안 줄바꿈이 새 행이 됨: 두 행 사이에 선이 없고 줄 간격만큼 떨어져 있다 (S1 Word '연락처' 행)."""
    cells = [_cx(0, 0, "연락처", 106, 140, y0=108, h=10), _cx(0, 1, "010-0000-", 166, 220, y0=108, h=10),
             _cx(0, 2, "test.il@example.c", 366, 450, y0=108, h=10),
             _cx(1, 1, "1234", 166, 190, y0=124, h=10), _cx(1, 2, "om", 366, 380, y0=124, h=10)]
    d = _doc_box(cells, 2, 3, b=145)
    pl = PageLines(height=H, h=[(Y[0], X[0], X[3]), (145, X[0], X[3])],
                   v=[(x, Y[0], 145) for x in (X[0], X[1], X[2], X[3])])
    log = fix_table_spans(d, {1: pl})
    assert _table(d).data.num_rows == 1
    assert _texts(d)[0] == ["연락처", "010-0000- 1234", "test.il@example.c om"]
    assert log.tables[0]["rows_joined"] == 1


def test_rows_far_apart_not_joined():
    """선이 없어도 두 행 글자가 줄 간격보다 멀리 떨어져 있으면 다른 행이다."""
    cells = [_cx(0, 0, "연락처", 106, 140, y0=108, h=10), _cx(0, 1, "010", 166, 220, y0=108, h=10),
             _cx(1, 1, "1234", 166, 190, y0=150, h=10)]
    d = _doc_box(cells, 2, 3, b=170)
    pl = PageLines(height=H, h=[(Y[0], X[0], X[3]), (170, X[0], X[3])],
                   v=[(x, Y[0], 170) for x in (X[0], X[1], X[2], X[3])])
    fix_table_spans(d, {1: pl})
    assert _table(d).data.num_rows == 2


def test_empty_value_cell_spans_where_no_vertical_line():
    """서식표의 빈 값 칸: 그 행 높이에서 열 경계 선이 없으면 빈 병합 칸 (S1 '사고 내용')."""
    cells = [_cell(0, 0, "보고 일시"), _cell(0, 1, "11.17"), _cell(0, 2, "담당"),
             _cell(1, 0, "사고 내용")]
    d = _doc(cells, 2)
    fix_table_spans(d, _lines(2, skip_v={(1, 1)}))
    assert _spans(d) == [(1, 1, 1, 2)]


def test_image_lines_found_and_text_strokes_ignored():
    """스캔 그림에서 긴 직선만 표 선으로 찾는다. 짧은 글자 획은 버린다."""
    import numpy as np

    from knowledge.ingestion.parsers.pdf.image_lines import _lines_from_image

    img = np.full((400, 400), 255, dtype=np.uint8)
    for x in (50, 150, 350):                     # 세로선 3개 (y 40~300)
        img[40:300, x:x + 2] = 0
    for y in (40, 170, 300):                     # 가로선 3개 (x 50~352)
        img[y:y + 2, 50:352] = 0
    img[100:102, 200:206] = 0                    # 짧은 획 (글자)
    img[95:105, 260:262] = 0
    pl = _lines_from_image(img, scale=1.0, height_pt=400)
    assert [round(x) for x in pl.verticals_in(50, 40, 352, 302)] == [51, 151, 351]
    ys = sorted({round(y) for y, x0, x1 in pl.h if x1 - x0 > 200})
    assert ys == [41, 171, 301]
    assert all(x1 - x0 >= 12 for _, x0, x1 in pl.h) and all(y1 - y0 >= 12 for _, y0, y1 in pl.v)


# ---------------------------------------------------------------- 2차 보정 (시험셋 v2 로그로 찾은 원인)
def test_dashed_line_pieces_chained():
    """점선 조각(짧은 대시) 여럿은 선 하나로, 떨어진 점 하나는 선이 아니다."""
    from knowledge.ingestion.parsers.pdf.pdf_lines import _chain_dashes

    dashes = [(100 + i * 4, 200, 102 + i * 4, 200.5) for i in range(20)]        # 2pt 대시, 2pt 틈 → 80pt 점선
    dashes.append((400, 300, 401, 301))                                         # 외딴 점
    lines = _chain_dashes(dashes, horizontal=True)
    assert len(lines) == 1
    y, x0, x1 = lines[0]
    assert round(y) == 200 and x0 == 100 and x1 == 178


def test_statistics_rows_with_label_not_joined():
    """첫 열에 이름이 있는 아래 행은 줄바꿈 조각이 아니라 진짜 행이다 (행 구분선이 안 잡힌 통계표)."""
    cells = [_cx(0, 0, "전금업 매출액", 106, 150, y0=108, h=10), _cx(0, 1, "12.0", 166, 190, y0=108, h=10),
             _cx(0, 2, "5.5", 366, 380, y0=108, h=10),
             _cx(1, 0, "(전자지급결제대행업)", 106, 158, y0=122, h=10), _cx(1, 1, "9.0", 166, 190, y0=122, h=10),
             _cx(1, 2, "4.1", 366, 380, y0=122, h=10)]
    d = _doc_box(cells, 2, 3, b=145)
    pl = PageLines(height=H, h=[(Y[0], X[0], X[3]), (145, X[0], X[3])],
                   v=[(x, Y[0], 145) for x in (X[0], X[1], X[2], X[3])])
    fix_table_spans(d, {1: pl})
    assert _table(d).data.num_rows == 2
