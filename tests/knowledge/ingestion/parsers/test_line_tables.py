# -*- coding: utf-8 -*-
"""선 기반 표 만들기 단위 테스트 (선·낱말을 직접 만든다)."""
from knowledge.ingestion.parsers.pdf.line_tables import build_grid, grid_table

XS = [100, 200, 300, 400]
YS = [100, 130, 160, 190]


def _grid_lines(skip_v=(), skip_h=()):
    """3x3 격자 선. skip_v: (행, 경계열) 빼기, skip_h: (경계행, 열) 빼기 — 그 칸 사이 선이 없음 = 병합."""
    hs, vs = [], []
    for r, y in enumerate(YS):
        for c in range(3):
            if (r, c) not in skip_h:
                hs.append((y, XS[c], XS[c + 1]))
    for c, x in enumerate(XS):
        for r in range(3):
            if (r, c) not in skip_v:
                vs.append((x, YS[r], YS[r + 1]))
    return hs, vs


def test_plain_grid():
    hs, vs = _grid_lines()
    ys, xs, cells = build_grid(hs, vs)
    assert (len(ys) - 1, len(xs) - 1, len(cells)) == (3, 3, 9)


def test_merged_cells_from_missing_lines():
    """0행: 1·2열 사이 세로선 없음 → 가로 병합. 0열: 1·2행 사이 가로선 없음 → 세로 병합."""
    hs, vs = _grid_lines(skip_v={(0, 2)}, skip_h={(2, 0)})
    _, _, cells = build_grid(hs, vs)
    assert (0, 1, 1, 3) in cells and (1, 3, 0, 1) in cells


def test_l_shape_gives_up():
    """선이 빠져 ㄱ자 칸이 생기면 포기한다(None)."""
    hs, vs = _grid_lines(skip_v={(0, 1)}, skip_h={(1, 1)})
    assert build_grid(hs, vs) is None


def test_words_placed_in_cells():
    hs, vs = _grid_lines(skip_v={(0, 2)})
    words = [("구분", 110, 105, 140, 117), ("결과", 210, 105, 240, 117), ("양호", 210, 135, 240, 147)]
    data = grid_table(hs, vs, words)
    grid = [[c.text for c in row] for row in data.grid]
    assert grid[0] == ["구분", "결과", "결과"] and grid[1][1] == "양호"


def test_words_on_same_line_keep_left_to_right_order():
    """높이가 조금 다른 낱말이 같은 줄에 있어도 왼→오른 순서 (예전 반올림 방식은 '취급작업은 중량물'로 뒤집었다)."""
    from knowledge.ingestion.parsers.pdf.table_backends.fill import order_words
    words = [("취급작업은", 160, 100.6, 220, 111.4), ("중량물", 110, 99.0, 150, 112.0), ("양호한가?", 110, 114, 170, 126)]
    assert [w[0] for w in order_words(words)] == ["중량물", "취급작업은", "양호한가?"]


def _doc_with_table(l, t, r, b, H=800):
    from docling_core.types.doc import (BoundingBox, CoordOrigin, DoclingDocument, ProvenanceItem, Size, TableCell,
                                        TableData)
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=600, height=H))
    cell = TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=1,
                     row_span=1, col_span=1, text="")   # 원래 표 글자 없음 → 글자 보존 안전장치는 막지 않는다
    prov = ProvenanceItem(page_no=1, charspan=(0, 0), bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT))
    d.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=[cell]), prov=prov)
    return d


def test_open_sided_table_left_alone():
    """Docling 표(40~460)보다 선 묶음(100~400)이 가로로 많이 좁으면 양옆이 트인 표 → 바꾸지 않는다."""
    from knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
    hs, vs = _grid_lines()
    d = _doc_with_table(40, 100, 460, 190)
    log = apply_line_tables(d, {1: PageLines(height=800, h=hs, v=vs)}, {1: [("구분", 110, 105, 140, 117)]})
    assert not log.replaced and "트인" in log.skipped[0]["why"]


def test_fully_ruled_table_replaced():
    from knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
    hs, vs = _grid_lines()
    d = _doc_with_table(98, 98, 402, 192)
    log = apply_line_tables(d, {1: PageLines(height=800, h=hs, v=vs)}, {1: [("구분", 110, 105, 140, 117)]})
    assert log.replaced and log.replaced[0]["after"] == "3x3"


def test_duplicate_text_inside_new_table_removed_and_box_updated():
    """Docling이 표 아래쪽만 표로 잡고(130~190) 위 줄 글자를 본문 글자로 따로 냈을 때:
    선 격자로 표 자리(100~190)를 고치고, 표 안에 이미 들어간 글자 항목은 지우며, 표 밖 글자는 남긴다."""
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, ProvenanceItem, TableItem, TextItem
    from knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines

    def prov(l, t, r, b):
        return ProvenanceItem(page_no=1, charspan=(0, 0), bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT))

    hs, vs = _grid_lines()
    d = _doc_with_table(98, 130, 402, 192)
    d.add_text(label=DocItemLabel.TEXT, text="점검자 : 홍길동", prov=prov(105, 102, 300, 126))   # 표 위 줄 (중복)
    d.add_text(label=DocItemLabel.TEXT, text="끝.", prov=prov(105, 300, 130, 312))             # 표 밖 (남김)
    words = [("점검자", 110, 105, 150, 117), (":", 152, 105, 156, 117), ("홍길동", 210, 105, 250, 117)]
    log = apply_line_tables(d, {1: PageLines(height=800, h=hs, v=vs)}, {1: words})
    texts = [it.text for it, _ in d.iterate_items() if isinstance(it, TextItem)]
    assert texts == ["끝."] and log.replaced[0]["dropped"] == ["점검자 : 홍길동"]
    t = next(it for it, _ in d.iterate_items() if isinstance(it, TableItem))
    b = t.prov[0].bbox.to_top_left_origin(800)
    assert (round(b.t), round(b.b)) == (100, 190)


def test_words_below_grid_inside_old_box_restored_after_table():
    """Docling 표 상자(100~260)가 표(100~190) 아래 서명란까지 잡았으면, 선 격자 밖 낱말을 표 뒤 본문 글자로 살린다."""
    from docling_core.types.doc import TableItem, TextItem
    from knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
    hs, vs = _grid_lines()
    d = _doc_with_table(98, 98, 402, 260)
    words = [("구분", 110, 105, 140, 117), ("확인자", 110, 230, 150, 242), ("(인)", 300, 230, 330, 242), ("반원", 110, 210, 140, 222)]
    log = apply_line_tables(d, {1: PageLines(height=800, h=hs, v=vs)}, {1: words})
    items = [it for it, _ in d.iterate_items()]
    assert isinstance(items[0], TableItem) and isinstance(items[1], TextItem)
    assert items[1].text == "반원\n확인자 (인)" and log.replaced[0]["restored"]


def test_table_kept_when_grid_loses_text():
    """낱말 위치가 선 격자 칸과 어긋나 새 표 글자가 거의 없으면(글자 보존 안 됨) 원래 표를 그대로 둔다."""
    from docling_core.types.doc import TableItem
    from knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
    hs, vs = _grid_lines()
    d = _doc_with_table(98, 98, 402, 192)
    t = next(it for it, _ in d.iterate_items() if isinstance(it, TableItem))
    t.data.table_cells[0].text = "임대료 산정 기준"
    words = [("임대료", 500, 600, 540, 612)]                  # 표 자리 밖 엉뚱한 위치 (좌표 어긋남)
    log = apply_line_tables(d, {1: PageLines(height=800, h=hs, v=vs)}, {1: words})
    assert not log.replaced and "글자 보존" in log.skipped[0]["why"]
    assert t.data.num_rows == 1 and t.data.table_cells[0].text == "임대료 산정 기준"


def test_one_side_open_table_left_alone(monkeypatch):
    """(가상 테두리를 끄면) 가로선이 왼쪽 세로선보다 밖으로 나가면(왼쪽 열에 세로선 없음) 한쪽이 트인 표 → 바꾸지 않는다."""
    import knowledge.ingestion.parsers.pdf.line_tables as lt
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
    monkeypatch.setattr(lt, "OPEN_EDGES", False)
    hs, vs = _grid_lines()
    hs = [(y, 60 if x0 == XS[0] else x0, x1) for y, x0, x1 in hs]      # 왼쪽으로 40pt 튀어나감
    d = _doc_with_table(98, 98, 402, 192)
    log = lt.apply_line_tables(d, {1: PageLines(height=800, h=hs, v=vs)}, {1: [("구분", 110, 105, 140, 117)]})
    assert not log.replaced and "한쪽이 트인" in log.skipped[0]["why"]


def test_one_side_open_table_closed_with_virtual_border():
    """가상 테두리(기본): 트인 왼쪽에 테두리를 합성해 왼쪽 열(60~100)까지 살린 선 격자 표로 바꾼다."""
    from knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
    hs, vs = _grid_lines()
    hs = [(y, 60 if x0 == XS[0] else x0, x1) for y, x0, x1 in hs]
    d = _doc_with_table(58, 98, 402, 192)
    words = [("번호", 65, 105, 95, 117), ("구분", 110, 105, 140, 117)]
    log = apply_line_tables(d, {1: PageLines(height=800, h=hs, v=vs)}, {1: words})
    rep = log.replaced[0]
    assert rep["virtual_edges"] and rep["after"].endswith(f"x{len(XS)}")


def test_thin_empty_top_row_dropped():
    """맨 위에 높이 10pt 빈 행(앞 쪽에서 넘어온 행 조각)이 있으면 버린다. 글자 있는 얇은 행은 남긴다."""
    from knowledge.ingestion.parsers.pdf.line_tables import grid_table
    ys = [90, 100, 130, 160]                               # 90~100: 얇은 빈 행
    hs = [(y, XS[0], XS[-1]) for y in ys]
    vs = [(x, ys[0], ys[-1]) for x in XS]
    words = [("구분", 110, 105, 140, 117), ("양호", 210, 135, 240, 147)]
    data = grid_table(hs, vs, words)
    assert data.num_rows == 2 and data.grid[0][0].text == "구분"     # 선 4개 = 3행 → 빈 조각 버리고 2행
    words2 = words + [("주", 110, 92, 118, 99)]
    assert grid_table(hs, vs, words2).num_rows == 3


def test_greedy_grid_handles_l_shaped_merge():
    """ㄱ자 병합(위 행 가운데 세로선 없음 + 왼쪽 열 가운데 가로선 없음): 우리 격자는 포기하고, kordoc 방식은 왼쪽 위부터 늘려 직사각형 칸."""
    from knowledge.ingestion.parsers.pdf.line_tables import build_grid, build_grid_greedy
    hs = [(100, 100, 300), (200, 100, 300), (150, 200, 300)]       # 가운데 가로선은 오른쪽 열에만
    vs = [(100, 100, 200), (300, 100, 200), (200, 150, 200)]       # 가운데 세로선은 아래 행에만
    assert build_grid(hs, vs) is None
    ys, xs, cells = build_grid_greedy(hs, vs)
    assert cells == [(0, 1, 0, 2), (1, 2, 0, 1), (1, 2, 1, 2)]


def test_greedy_grid_needs_no_outer_border():
    """바깥 왼쪽 테두리가 거의 없어도 칸을 만든다 (우리 격자는 네 변 80%를 요구)."""
    from knowledge.ingestion.parsers.pdf.line_tables import build_grid, build_grid_greedy
    hs = [(100, 100, 300), (150, 100, 300), (200, 100, 300)]
    vs = [(100, 100, 110), (200, 100, 200), (300, 100, 200)]       # 왼쪽 세로선은 10pt 토막뿐
    assert build_grid(hs, vs) is None
    ys, xs, cells = build_grid_greedy(hs, vs)
    assert len(cells) == 4


def test_picture_without_text_inside_new_table_kept():
    """선 격자 표 자리 안에 있어도 글자 없는 그림(사진·그래프)은 중복으로 지우지 않는다."""
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, PictureItem, ProvenanceItem, TableItem
    from knowledge.ingestion.parsers.pdf.line_tables import _drop_duplicates
    d = _doc_with_table(98, 98, 402, 192)
    t = next(it for it, _ in d.iterate_items() if isinstance(it, TableItem))
    d.add_picture(prov=ProvenanceItem(page_no=1, charspan=(0, 0), bbox=BoundingBox(l=110, t=110, r=190, b=180, coord_origin=CoordOrigin.TOPLEFT)))
    dropped = _drop_duplicates(d, t, 1, (98, 98, 402, 192))
    assert dropped == [] and any(isinstance(it, PictureItem) for it, _ in d.iterate_items())


# ---- 한컴 PDF 대비책: 칸 클립 혼합 뒤 표 모델 그대로 남은 표에만 늘려 만들기 격자 (2026-11-27)
def _fallback_case():
    """바닥 테두리가 반만 그려진 2x2 표 → 엄격 격자는 포기, 늘려 만들기 격자는 만든다."""
    from docling_core.types.doc import (BoundingBox, CoordOrigin, DoclingDocument, ProvenanceItem, Size, TableCell,
                                        TableData)
    from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=600, height=800))
    cells = [TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=2,
                       row_span=1, col_span=2, text="가 나 다 라")]
    d.add_table(data=TableData(num_rows=1, num_cols=2, table_cells=cells),
                prov=ProvenanceItem(page_no=1, charspan=(0, 0), bbox=BoundingBox(l=50, t=100, r=500, b=200, coord_origin=CoordOrigin.TOPLEFT)))
    hs = [(100, 50, 500), (150, 50, 500), (200, 50, 300)]
    vs = [(50, 100, 200), (275, 100, 200), (500, 100, 200)]
    words = {1: [("가", 60, 110, 80, 130), ("나", 300, 110, 320, 130), ("다", 60, 160, 80, 180), ("라", 300, 160, 320, 180)]}
    return d, {1: PageLines(height=800, h=hs, v=vs)}, words


def test_fallback_greedy_only_for_given_tables():
    from knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
    d, lines, words = _fallback_case()
    assert not apply_line_tables(d, lines, words, relaxed=False).replaced                  # 한컴 첫 호출: 엄격만 → 포기
    assert not apply_line_tables(d, lines, words, relaxed=False, only=set(), fallback=True).replaced   # 대상 아님
    log = apply_line_tables(d, lines, words, relaxed=False, only={id(d.tables[0])}, fallback=True)
    assert log.replaced and log.replaced[0].get("greedy") and d.tables[0].data.num_rows == 2
