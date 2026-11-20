# -*- coding: utf-8 -*-
"""한 쪽 안의 표에서 표 모델이 놓친 병합 칸을 되살린다 (Docling 결과 후처리).

왜 필요한가
  dev 진단: 행·열 수는 맞는데 병합이 틀린 표의 대부분이 '세로 병합을 두 칸으로 읽음'이었다.
    - 2줄 머리글: '영역·번호·점검 항목'처럼 두 줄 높이 칸을 위 칸(글자) + 아래 칸(빈 칸)으로 읽음
    - 묶음 열: '정 보'(3행), '시 / 스 템'(2행)처럼 여러 행 높이 칸을 행마다 따로 읽음 (글자가 쪼개지기도 함)
    - 서식표: 빈 값 칸이 세 열에 걸쳐 있는데 열마다 따로 읽음 (가로 병합)

방법 1 — 선 (글자층 PDF)
  표 테두리는 PDF에 실제 선으로 있다(pdf_lines). 칸 경계에 선이 없으면 두 칸은 한 칸이다.
    - 세로: r행과 r+1행 사이(글자 띠 사이 틈)에 c열 폭의 절반 이상을 덮는 가로선이 없으면 이어진 칸
    - 가로: r행 글자 띠 높이에서 c열과 c+1열 경계 x에 세로선이 없으면 이어진 칸
  선이 빠진 이유가 '병합'이라고 믿으려면 표 전체에 선이 그려져 있어야 한다:
    모든 행 사이 틈에 가로선이 어딘가에는 있고, 모든 열 경계 x에 세로선이 어딘가에는 있을 때만 쓴다.
    (선 없는 표·가로줄만 있는 표에서 모든 칸이 합쳐지는 사고를 막는다.)

방법 2 — 머리글 구조 (스캔처럼 선이 없을 때)
  첫 행에 여러 열에 걸친 칸(예: '결과' 위에 '양호·미흡')이 있고 둘째 행에 그 아래 칸들이 있으면 2줄 머리글이다.
  2줄 머리글에서 첫 행의 한 열짜리 칸은 아래 칸과 한 칸이다 (한 열짜리 부모에 자식 하나는 의미가 없다).
  선이 있는 표라도 선으로 판단하지 못한 열(안쪽 선이 하나도 안 잡힌 열)에는 이 규칙을 쓴다.
  → 아래 칸이 비었으면 위 칸을 2행으로 늘리고, 글자가 있으면(세로쓰기가 쪼개진 '대 / 분 류') 이어 붙인다.

한계
  - 선이 없는 표의 본문 병합(묶음 열)은 판단하지 않는다.
  - 여러 쪽에 걸친 표는 쪽마다 따로 고친 다음 합친다 (table_merge보다 먼저 부른다).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent_platform.knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
from agent_platform.knowledge.ingestion.parsers.pdf.table_merge import LINE_MIN_COVER, _box, _col_starts
from docling_core.types.doc import BoundingBox, DoclingDocument, TableCell, TableData, TableItem

GAP_TOL = 1.5        # 행 사이 틈을 이만큼 넓혀서 가로선을 찾는다 (pt)
X_TOL = 2.0          # 열 경계 x와 세로선 x 허용 차이 (pt)
COVER = 0.5          # 선이 칸 폭(가로선)·글자 띠 높이(세로선)의 이 비율 이상을 덮어야 경계로 본다
PIECE_MAX = 3        # 글자 있는 칸 여럿을 이을 때, 조각마다 이 글자 수(공백 뺌) 이하여야 한다 (세로쓰기 '분 류')
PIECE_WIDE = 1.3     # 조각 글자 bbox가 폭 > 높이 × 이 값이면 가로쓰기 → 세로쓰기 조각이 아니다


def _vertical_piece(c: TableCell) -> bool:
    """세로쓰기가 쪼개진 조각처럼 생겼나: 짧고(3글자 이하), 글자 bbox가 옆으로 넓지 않다.

    S1 '대 / 분 류', '시 / 스 템'은 한 글자 폭으로 세로로 쌓인 글자다.
    붙임4 서명란 '반원 / 반장'은 짧지만 가로로 쓴 별개 행이다 → 폭으로 걸러낸다.
    """
    if len("".join(c.text.split())) > PIECE_MAX or c.bbox is None:
        return False
    b = c.bbox
    w, h = abs(b.r - b.l), abs(b.b - b.t)
    return w <= PIECE_WIDE * h


def _pieces_ok(parts: list[TableCell]) -> bool:
    """이어도 되는 조각들인가: 글자 있는 칸이 하나뿐이거나, 모두 세로쓰기 조각일 때만.

    글자 칸 둘 이상을 함부로 잇지 않는 이유: 음영 칸 등에서 선이 안 잡혀 '선 없음'이 거짓일 수 있다.
    dev 신구조문 대조표에서 서로 다른 '사유' 두 칸, 머리글 '준칙 조항 | 검토의견'이 잘못 이어질 뻔했다.
    """
    texts = [p for p in parts if p.text.strip()]
    return len(texts) <= 1 or all(_vertical_piece(p) for p in texts)


@dataclass
class SpanLog:
    tables: list[dict] = field(default_factory=list)


def _tl(doc, t, bb):
    """칸 bbox를 왼쪽 위 원점으로."""
    if bb is None:
        return None
    if bb.coord_origin.value == "TOPLEFT":
        return bb
    return bb.to_top_left_origin(doc.pages[t.prov[0].page_no].size.height)


def _union(a: BoundingBox | None, b: BoundingBox | None) -> BoundingBox | None:
    if a is None or b is None or a.coord_origin != b.coord_origin:
        return a or b
    return BoundingBox(l=min(a.l, b.l), t=min(a.t, b.t), r=max(a.r, b.r), b=max(a.b, b.b), coord_origin=a.coord_origin)


def _covering(cells: list[TableCell], r: int, c: int) -> TableCell | None:
    for x in cells:
        if x.start_row_offset_idx <= r < x.end_row_offset_idx and x.start_col_offset_idx <= c < x.end_col_offset_idx:
            return x
    return None


def _join(parts: list[TableCell], rows: tuple[int, int], cols: tuple[int, int]) -> TableCell:
    """여러 칸을 한 칸으로: 글자는 읽기 순서(위→아래, 왼→오른)로 잇고 bbox는 합친다."""
    parts = sorted(parts, key=lambda x: (x.start_row_offset_idx, x.start_col_offset_idx))
    text = " ".join(p.text.strip() for p in parts if p.text.strip())
    bb = None
    for p in parts:
        bb = _union(bb, p.bbox) if bb is not None else p.bbox
    base = next((p for p in parts if p.text.strip()), parts[0])
    return base.model_copy(update={
        "start_row_offset_idx": rows[0], "end_row_offset_idx": rows[1], "row_span": rows[1] - rows[0],
        "start_col_offset_idx": cols[0], "end_col_offset_idx": cols[1], "col_span": cols[1] - cols[0],
        "text": text, "bbox": bb,
    })


def _merge_runs(cells: list[TableCell], n_rows: int, n_cols: int, cut, vertical: bool,
                allow_empty: bool = False) -> int:
    """경계 cut(i, j)가 False인 이웃 칸을 이어 붙인다. 이은 횟수를 돌려준다.

    vertical=True : 열 j마다 행 i, i+1 사이에 경계가 없으면 이음
    vertical=False: 행 i마다 열 j, j+1 사이에 경계가 없으면 이음
    한 덩어리(run) 안의 칸이 모두 같은 폭(세로)·같은 높이(가로)일 때만 잇는다 — 모양이 어긋나면 표 모델 판단을 존중.
    allow_empty: 글자가 하나도 없는 덩어리도 빈 병합 칸으로 만든다 (서식표의 빈 값 칸. 가로에만 쓴다).
    """
    count = 0
    outer, inner = (n_cols, n_rows) if vertical else (n_rows, n_cols)
    for o in range(outer):
        i = 0
        while i < inner:
            e = i
            while e + 1 < inner and cut(e, o) is False:
                e += 1
            if e > i:
                at = (lambda k: _covering(cells, k, o)) if vertical else (lambda k: _covering(cells, o, k))
                parts, ok = [], True
                for k in range(i, e + 1):
                    x = at(k)
                    if x is None or x in parts:
                        continue
                    if vertical:
                        ok &= x.start_row_offset_idx >= i and x.end_row_offset_idx <= e + 1
                    else:
                        ok &= x.start_col_offset_idx >= i and x.end_col_offset_idx <= e + 1
                    parts.append(x)
                if ok and allow_empty and not any(p.text.strip() for p in parts):
                    # 빈 덩어리: 표 모델이 칸을 안 만들었거나 빈 칸만 있다 → 빈 병합 칸 하나로
                    if all((p.start_row_offset_idx, p.end_row_offset_idx) == (o, o + 1) for p in parts):
                        for p in parts:
                            cells.remove(p)
                        cells.append(TableCell(start_row_offset_idx=o, end_row_offset_idx=o + 1, start_col_offset_idx=i,
                                               end_col_offset_idx=e + 1, row_span=1, col_span=e + 1 - i, text=""))
                        count += 1
                elif ok and parts and any(p.text.strip() for p in parts) and _pieces_ok(parts):
                    span = {(p.start_col_offset_idx, p.end_col_offset_idx) if vertical
                            else (p.start_row_offset_idx, p.end_row_offset_idx) for p in parts}
                    if len(span) == 1:
                        (a, b), = span
                        rows, cols = ((i, e + 1), (a, b)) if vertical else ((a, b), (i, e + 1))
                        whole = (rows[1] - rows[0], cols[1] - cols[0])
                        if len(parts) > 1 or (whole != (parts[0].row_span, parts[0].col_span)):
                            new = _join(parts, rows, cols)
                            for p in parts:
                                cells.remove(p)
                            cells.append(new)
                            count += 1
            i = e + 1
    return count


def _row_bands(doc, t: TableItem) -> list[tuple[float, float] | None]:
    """행마다 글자 띠 (위, 아래). 한 행짜리 칸만 쓴다."""
    bands: list[tuple[float, float] | None] = [None] * t.data.num_rows
    for c in t.data.table_cells:
        if c.row_span != 1 or not c.text.strip():
            continue
        b = _tl(doc, t, c.bbox)
        if b is None:
            continue
        r = c.start_row_offset_idx
        lo, hi = bands[r] or (b.t, b.b)
        bands[r] = (min(lo, b.t), max(hi, b.b))
    return bands


def _fix_by_lines(doc, t: TableItem, pl: PageLines, debug: dict | None = None) -> tuple[int, set[int]] | None:
    """방법 1. (이은 횟수, 선으로 세로 판단을 한 열 번호). 선을 못 믿을 표면 None."""
    cs = _col_starts(doc, t, t.prov[0], pl)
    if cs is None:
        return None
    starts, xs, right = cs
    if any(x is None for x in starts):
        return None
    edges = starts + [right]                                   # 열 c의 x 범위 = edges[c] ~ edges[c+1]
    bands = _row_bands(doc, t)
    if any(b is None for b in bands):
        return None
    box, _ = _box(doc, t.prov[0])
    hs = [h for h in pl.h if box.l - 2 <= h[1] <= box.r + 2 or box.l - 2 <= h[2] <= box.r + 2 or h[1] <= box.l <= h[2]]
    vs = pl.v

    nr, nc = t.data.num_rows, t.data.num_cols
    src = list(t.data.table_cells)

    def text_box(r, c):
        """(r, c)를 덮는 칸의 글자 bbox. 빈 칸·없는 칸이면 None."""
        x = _covering(src, r, c)
        return _tl(doc, t, x.bbox) if x is not None and x.text.strip() and x.bbox is not None else None

    def h_cut(r, c):
        """r행과 r+1행 사이, c열 폭에 가로선이 있나.

        틈은 그 열의 두 칸 글자 사이로 잡는다(없으면 행 전체 글자 띠). 행 전체 띠만 쓰면
        세로쓰기 '분 류'처럼 위로 튀어나온 글자 때문에 틈이 사라진다 (S1 Word PDF).
        """
        up, dn = text_box(r, c), text_box(r + 1, c)
        y0 = (up.b if up is not None else bands[r][1]) - GAP_TOL
        y1 = (dn.t if dn is not None else bands[r + 1][0]) + GAP_TOL
        if y1 <= y0:
            return None                                        # 글자가 겹침 → 판단 불가(경계로 취급)
        w = edges[c + 1] - edges[c]
        cov = sum(max(0.0, min(x1, edges[c + 1]) - max(x0, edges[c])) for y, x0, x1 in hs if y0 <= y <= y1)
        return bool(cov >= COVER * w)

    def v_cut(r, c):
        """r행에서 c열과 c+1열 경계에 세로선이 있나 (두 칸 글자 높이, 없으면 행 글자 띠 기준)."""
        boxes = [b for b in (text_box(r, c), text_box(r, c + 1)) if b is not None]
        t0, t1 = (min(b.t for b in boxes), max(b.b for b in boxes)) if boxes else bands[r]
        x = edges[c + 1]
        cov = sum(max(0.0, min(y1, t1) - max(y0, t0)) for vx, y0, y1 in vs if abs(vx - x) <= X_TOL)
        return bool(cov >= COVER * max(t1 - t0, 1.0))

    if debug is not None:                                      # 진단용: 경계 판단 표
        debug["h"] = [[h_cut(r, c) for c in range(nc)] for r in range(nr - 1)]
        debug["v"] = [[v_cut(r, c) for c in range(nc - 1)] for r in range(nr)]
    # 표 전체에 선이 그려져 있는지 본다.
    #   행 사이 틈마다 가로선이 어딘가 있으면 '아는 틈'. 하나도 없으면 '모르는 틈'(음영에 묻힌 선 등) → 그 틈은 잇지 않는다.
    #   아는 틈이 절반 이상이어야 표의 선을 믿는다 (S1 인쇄 PDF: 머리글 줄 아래 선만 음영에 묻힘).
    gap_known = [any(h_cut(r, c) for c in range(nc)) for r in range(nr - 1)]
    col_known = [any(v_cut(r, c) for r in range(nr)) for c in range(nc - 1)]
    if nr > 1 and sum(gap_known) * 2 < (nr - 1):
        return None
    if nc > 1 and sum(col_known) * 2 < (nc - 1):
        return None
    # 열(행) 안쪽에 선이 하나도 안 잡히면 그 열(행)은 선을 못 믿는다 → 경계 있음으로 취급
    #   S2 인쇄 PDF: 회색 음영 이름표 열의 행 구분선이 음영 사각형에 묻혀 안 잡힌다 → 열 전체가 한 칸이 되는 사고
    h_ok = [nr < 2 or any(h_cut(r, c) for r in range(nr - 1)) for c in range(nc)]
    v_ok = [nc < 2 or any(v_cut(r, c) for c in range(nc - 1)) for r in range(nr)]

    cells = list(t.data.table_cells)
    # cut(i, o): True=경계 있음, False=경계 없음(이음), None=판단 불가(경계로 본다 — _merge_runs는 False일 때만 잇는다)
    n = _merge_runs(cells, nr, nc, lambda i, o: h_cut(i, o) if (h_ok[o] and gap_known[i]) else True, vertical=True)
    n += _merge_runs(cells, nr, nc, lambda i, o: v_cut(o, i) if (v_ok[o] and col_known[i]) else True, vertical=False,
                     allow_empty=True)
    if n:
        t.data = TableData(num_rows=nr, num_cols=nc, table_cells=cells)
    # 머리글 규칙(첫 두 행)에 넘길 '선으로 이미 판단한 열': 첫 틈을 아는 경우에만
    decided = {c for c in range(nc) if h_ok[c]} if (nr > 1 and gap_known[0]) else set()
    return n, decided


def _fix_header(t: TableItem, skip: set[int] = frozenset()) -> int:
    """방법 2. 2줄 머리글의 한 열짜리 칸을 아래 칸과 합친다. skip: 선으로 이미 판단한 열(건드리지 않음)."""
    d = t.data
    if d.num_rows < 2:
        return 0
    row0 = [c for c in d.table_cells if c.start_row_offset_idx == 0]
    row1 = [c for c in d.table_cells if c.start_row_offset_idx == 1 and c.row_span == 1]
    parents = [p for p in row0 if p.col_span >= 2 and p.row_span == 1 and p.text.strip()]
    # 2줄 머리글 증거: 첫 행의 여러 열 칸 아래에 둘째 행 칸이 2개 이상, 글자가 있다
    if not any(sum(1 for k in row1 if p.start_col_offset_idx <= k.start_col_offset_idx < p.end_col_offset_idx
                   and k.text.strip()) >= 2 for p in parents):
        return 0
    cells = list(d.table_cells)
    cand = [top for top in row0 if top.col_span == 1 and top.row_span == 1 and top.text.strip()
            and top.start_col_offset_idx not in skip]
    # 진짜 2줄 머리글이면 한 열짜리 머리글 칸 아래는 대부분 비어 있다.
    # 아래가 대부분 차 있으면 둘째 행은 데이터 행이다 (dev InDesign 모집요강: '연번 | 1'이 이어질 뻔함).
    below = [_covering(cells, 1, top.start_col_offset_idx) for top in cand]
    empty = sum(1 for x in below if x is None or not x.text.strip())
    if not cand or empty * 2 <= len(cand):
        return 0
    n = 0
    for top, low in zip(cand, below):
        c = top.start_col_offset_idx
        if low is not None and (low.col_span != 1 or low.row_span != 1):
            continue
        parts = [top] + ([low] if low is not None else [])
        if low is not None and low.text.strip() and not (_vertical_piece(low) and _vertical_piece(top)):
            continue                                    # 아래 칸에 글자가 있으면 세로쓰기 조각일 때만 잇는다
        new = _join(parts, (0, 2), (c, c + 1))
        for p in parts:
            cells.remove(p)
        cells.append(new)
        n += 1
    if n:
        t.data = TableData(num_rows=d.num_rows, num_cols=d.num_cols, table_cells=cells)
    return n


def _rebuild(cells: list[TableCell], rmap: list[int], cmap: list[int], nr: int, nc: int) -> TableData | None:
    """행·열 번호를 rmap·cmap으로 바꾸고, 같은 자리에 온 칸은 글자를 이어 한 칸으로. 자리가 어긋나게 겹치면 None."""
    placed: dict[tuple, list[TableCell]] = {}
    for c in cells:
        key = (rmap[c.start_row_offset_idx], rmap[c.end_row_offset_idx - 1] + 1,
               cmap[c.start_col_offset_idx], cmap[c.end_col_offset_idx - 1] + 1)
        placed.setdefault(key, []).append(c)
    keys = list(placed)
    for a in range(len(keys)):                                  # 서로 다른 자리끼리 겹치면 포기
        for b in range(a + 1, len(keys)):
            ka, kb = keys[a], keys[b]
            if ka[0] < kb[1] and kb[0] < ka[1] and ka[2] < kb[3] and kb[2] < ka[3]:
                return None
    new = [_join(ps, (k[0], k[1]), (k[2], k[3])) for k, ps in placed.items()]
    return TableData(num_rows=nr, num_cols=nc, table_cells=new)


def _col_centers(doc, t: TableItem) -> dict[int, float]:
    """열마다 한 열짜리 칸 글자 가운데 x의 중앙값."""
    xs: dict[int, list[float]] = {}
    for c in t.data.table_cells:
        b = _tl(doc, t, c.bbox)
        if c.col_span == 1 and c.text.strip() and b is not None:
            xs.setdefault(c.start_col_offset_idx, []).append((b.l + b.r) / 2)
    return {k: sorted(v)[len(v) // 2] for k, v in xs.items()}


def _collapse_split_columns(doc, t: TableItem, pl: PageLines) -> int:
    """같은 세로선에서 시작하는 이웃 열 = 표 모델이 한 열을 쪼갠 것 → 한 열로 합친다. 합쳐 없앤 열 수.

    dev 붙임2: 머리글 '지 원 기 관'의 띄어 쓴 글자마다 열을 만들어 4열 → 7열이 됐다. 네 열 모두 x=375 선에서 시작.
    """
    cs = _col_starts(doc, t, t.prov[0], pl)
    if cs is None:
        return 0
    starts, xs, _right = cs
    if any(x is None for x in starts):
        return 0
    centers = _col_centers(doc, t)
    cmap, k = [0], 0
    for c, (a, b) in enumerate(zip(starts, starts[1:])):
        ca, cb = centers.get(c), centers.get(c + 1)
        line_between = ca is not None and cb is not None and any(min(ca, cb) < x < max(ca, cb) for x in xs)
        if abs(b - a) > X_TOL or line_between:          # 시작 선이 다르거나, 두 열 글자 사이에 선이 있으면 다른 열
            k += 1
        cmap.append(k)
    nc = k + 1
    if nc == t.data.num_cols:
        return 0
    nr = t.data.num_rows
    data = _rebuild(list(t.data.table_cells), list(range(nr)), cmap, nr, nc)
    if data is None:
        return 0
    removed = t.data.num_cols - nc
    t.data = data
    return removed


def _char_count(cells: list[TableCell]) -> dict[str, int]:
    """칸 글자를 공백 빼고 글자별로 센다 (표를 고친 뒤 글자가 사라지거나 생기지 않았는지 확인용)."""
    out: dict[str, int] = {}
    for c in cells:
        for ch in "".join(c.text.split()):
            out[ch] = out.get(ch, 0) + 1
    return out


def _split_fused_columns(doc, t: TableItem, pl: PageLines, words: list | None) -> int:
    """표 모델이 두 열을 한 열로 읽은 것을 세로선으로 나눈다. 늘어난 열 수.

    한 열의 x 범위 안쪽에 세로선(표 높이의 10% 이상)이 있으면 그 x를 새 열 경계로 넣는다.
      - 그 경계를 지나는 칸이 자기 행 높이에서 그 선에 막혀 있으면 → 실제로는 두 칸 → 낱말 위치로 글자를 나눈다
      - 막혀 있지 않으면 → 두 열에 걸친 병합 칸
    낱말 위치가 없는데(스캔) 나눠야 하면 아무것도 안 한다.
    dev S1 서식표: '보고 자 | 테스트일 (정보보호팀)'이 한 칸, 그 아래 행은 세 열에 걸친 값 칸.
    """
    cs = _col_starts(doc, t, t.prov[0], pl)
    if cs is None or any(x is None for x in cs[0]):
        return 0
    starts, _xs, right = cs
    box, _ = _box(doc, t.prov[0])
    xs = pl.verticals_in(box.l, box.t, box.r, box.b, min_cover=LINE_MIN_COVER)

    def touches_border(x):
        """세로선이 표 위 또는 아래 테두리에 닿는가. 칸 안에 그린 작은 표의 선은 테두리에 안 닿는다
        (dev 신구조문 대조표 16쪽: 칸 안 '별표' 작은 표의 선 때문에 3열이 7열로 쪼개질 뻔함)."""
        ys = [(y0, y1) for vx, y0, y1 in pl.v if abs(vx - x) <= X_TOL]
        return any(y0 <= box.t + BORDER_TOL for y0, _ in ys) or any(y1 >= box.b - BORDER_TOL for _, y1 in ys)

    old_edges = starts + [right]
    inner = [x for c in range(len(starts)) for x in xs
             if old_edges[c] + X_TOL < x < old_edges[c + 1] - X_TOL and touches_border(x)]
    if not inner:
        return 0
    edges = sorted(starts + inner)

    def idx(x):
        return min(range(len(edges)), key=lambda k: abs(edges[k] - x))

    new_cells = []
    for c in t.data.table_cells:
        s0 = idx(old_edges[c.start_col_offset_idx])
        e0 = idx(old_edges[c.end_col_offset_idx]) if c.end_col_offset_idx < len(starts) else len(edges)
        b = _tl(doc, t, c.bbox)
        if e0 - s0 == 1 or b is None or not c.text.strip():
            if b is not None and c.text.strip():
                lo_x, hi_x = edges[s0], (edges[e0] if e0 < len(edges) else right)
                if lo_x > b.l + 4.0 or hi_x < b.r - 4.0:
                    return 0                        # 글자가 새 칸 밖으로 삐져나감 → 표 모델 열이 엉켜 있다 (신구조문 18쪽)
            new_cells.append(c.model_copy(update={"start_col_offset_idx": s0, "end_col_offset_idx": e0,
                                                  "col_span": e0 - s0}))
            continue
        # 이 칸 행 높이(글자 위~아래)에서 새 경계 x마다 세로선이 막고 있는지
        cuts = []
        for k in range(s0 + 1, e0):
            x = edges[k]
            cov = sum(max(0.0, min(y1, b.b) - max(y0, b.t)) for vx, y0, y1 in pl.v if abs(vx - x) <= X_TOL)
            if cov >= COVER * max(b.b - b.t, 1.0):
                cuts.append(k)
        bounds = [s0] + cuts + [e0]
        segs = [(a, z) for a, z in zip(bounds, bounds[1:])]   # 막힌 경계로 나눈 조각 (조각 안은 병합 칸)

        def seg_x(a, z):
            return edges[a], (edges[z] if z < len(edges) else right)

        if len(segs) == 1:                          # 막힌 경계가 없다 → 여러 열에 걸친 병합 칸
            a, z = segs[0]
            if seg_x(a, z)[0] > b.l + 4.0 or seg_x(a, z)[1] < b.r - 4.0:
                return 0                            # 글자가 새 칸 밖으로 삐져나감 → 열 위치를 잘못 잡았다 (신구조문 18쪽)
            new_cells.append(c.model_copy(update={"start_col_offset_idx": a, "end_col_offset_idx": z, "col_span": z - a}))
            continue
        # 글자를 조각에 나눠 담는다. 글자층 낱말 위치가 있으면 그것을 쓴다(표 모델 칸 bbox는 몇 pt 어긋남 — 신구조문 18쪽).
        inside = [w for w in (words or []) if b.l - 2 <= (w[1] + w[3]) / 2 <= b.r + 2 and b.t - 2 <= (w[2] + w[4]) / 2 <= b.b + 2]
        if not inside or "".join("".join(w[0] for w in inside).split()) != "".join(c.text.split()):
            # 글자층 쪽인데 낱말이 칸 글자와 다르면 표 모델 칸이 엉킨 것 → 나누지 않는다 (신구조문 18쪽)
            if words:
                return 0
            # 스캔(낱말 위치 없음): 칸 bbox가 한 조각 안에만 있을 때만 그 조각에 둔다
            hit = [(a, z) for a, z in segs if min(b.r, seg_x(a, z)[1]) - max(b.l, seg_x(a, z)[0]) > 1.0]
            if len(hit) != 1:
                return 0
            a, z = hit[0]
            if seg_x(a, z)[0] > b.l + 4.0 or seg_x(a, z)[1] < b.r - 4.0:
                return 0
            new_cells.append(c.model_copy(update={"start_col_offset_idx": a, "end_col_offset_idx": z, "col_span": z - a}))
            continue
        for a, z in segs:
            lo_x, hi_x = seg_x(a, z)
            part = [w for w in inside if lo_x <= (w[1] + w[3]) / 2 < hi_x]
            if not part:
                continue
            part.sort(key=lambda w: (round(w[2]), w[1]))
            bb = BoundingBox(l=min(w[1] for w in part), t=min(w[2] for w in part), r=max(w[3] for w in part),
                             b=max(w[4] for w in part), coord_origin=b.coord_origin)
            new_cells.append(c.model_copy(update={"start_col_offset_idx": a, "end_col_offset_idx": z, "col_span": z - a,
                                                  "text": " ".join(w[0] for w in part), "bbox": bb}))
    new = TableData(num_rows=t.data.num_rows, num_cols=len(edges), table_cells=new_cells)
    # 안전장치 (dev 정답 없는 문서에서 잘못 쪼갠 사례로 정함):
    #   1) 새로 생긴 열마다 글자 있는 칸이 하나는 있어야 한다 (빈 열이 생기면 선을 잘못 본 것 — 신구조문 16·18쪽)
    #   2) 표 전체 글자(글자 수 세기)가 그대로여야 한다 (InDesign 3쪽: 머리글 숫자 '2'가 사라짐)
    used = {k for c in new_cells if c.text.strip() for k in range(c.start_col_offset_idx, c.end_col_offset_idx)}
    if any(k not in used for k in range(len(edges))):
        return 0
    if _char_count(t.data.table_cells) != _char_count(new_cells):
        return 0
    t.data = new
    return len(inner)


BORDER_TOL = 8.0      # 세로선이 표 위·아래 테두리에 '닿는다'고 볼 거리 (Word는 칸 여백만큼 떨어져 그린다: 4.5pt)
LINE_H_RATIO = 1.8    # 줄바꿈 판단: 두 행 글자 사이 틈 ≤ 한 줄 높이 × 이 값 (인쇄 PDF 글자 상자가 작아 1.56배까지 나옴)


def _collapse_wrapped_rows(doc, t: TableItem, pl: PageLines) -> int:
    """칸 안 줄바꿈을 표 모델이 새 행으로 읽은 것을 되돌린다. 없앤 행 수.

    조건 (모두):
      - 두 행 글자 사이 틈에 표 폭 어디에도 가로선이 없다
      - 표 위·아래 테두리 가로선이 표 폭의 절반 이상 있다 (선이 그려진 표)
      - 표가 2행이거나, 다른 행 사이에는 가로선이 있다 (행 구분선이 아예 없는 표는 건드리지 않음)
      - 틈이 한 줄 높이의 1.8배 이하 (줄 간격)
      - 아래 행은 위 행에 글자가 있는 열에만 글자가 있다 (이어지는 글자)
      - 아래 행의 첫 열(이름표 열)은 비어 있고, 채워진 칸 수가 위 행보다 적다
    dev S1 Word·인쇄: '연락처 | 010-0000- | 전자 | test.il@example.c' 아래 '1234 | 우편 | om'이 새 행이 됐다.
    """
    d = t.data
    nr = d.num_rows
    if nr < 2:
        return 0
    box, _ = _box(doc, t.prov[0])
    width = box.r - box.l

    def has_h(y0, y1, frac):
        cov = sum(max(0.0, min(x1, box.r) - max(x0, box.l)) for y, x0, x1 in pl.h if y0 <= y <= y1)
        return cov >= frac * width

    # 위·아래 테두리: 표 폭의 절반 이상 (인쇄 PDF는 음영 칸 위 테두리가 묻혀 일부만 잡힌다)
    if not (has_h(box.t - 4, box.t + 4, 0.5) and has_h(box.b - 4, box.b + 4, 0.5)):
        return 0
    cells = list(d.table_cells)
    rows = [[c for c in cells if c.start_row_offset_idx == r and c.row_span == 1 and c.text.strip() and c.bbox]
            for r in range(nr)]
    tb = [[_tl(doc, t, c.bbox) for c in row] for row in rows]
    gaps = []
    for r in range(nr - 1):
        if not tb[r] or not tb[r + 1]:
            gaps.append(None)
            continue
        # 선 찾는 창은 넓게(위 행 아래끝 중 가장 위 ~ 아래 행 위끝 중 가장 아래): 세로쓰기 글자('스 템')가
        # 행 경계를 넘어 튀어나와도 선을 놓치지 않게. 줄 간격은 좁게(가장 아래 ~ 가장 위)로 잰다.
        lo, hi = min(b.b for b in tb[r]), max(b.t for b in tb[r + 1])
        y0, y1 = max(b.b for b in tb[r]), min(b.t for b in tb[r + 1])
        lined = has_h(min(lo, hi) - GAP_TOL, max(lo, hi) + GAP_TOL, 0.0001)
        gaps.append((y0, y1, lined or y1 <= y0))
    lined = sum(1 for g in gaps if g and g[2])
    rmap, k = [0], 0
    merged = 0
    for r, g in enumerate(gaps):
        wrap = False
        if g is not None and not g[2] and (nr == 2 or lined > 0):
            line_h = min(b.b - b.t for b in tb[r] + tb[r + 1])
            up_cols = {c.start_col_offset_idx for c in rows[r]}
            dn_cols = {c.start_col_offset_idx for c in rows[r + 1]}
            only_spans = all(c.row_span == 1 for c in cells if c.start_row_offset_idx in (r, r + 1))
            # 첫 열(이름표 열)이 비어 있고 위 행보다 채워진 칸이 적어야 '이어진 글자'로 본다.
            # (위 조건만으로는 행 구분선이 안 잡힌 통계표의 진짜 행 — '(전자지급결제대행업) 9.0 4.1 …' 처럼
            #  첫 열에 이름이 있는 행 — 까지 합쳤다. 시험셋 v2 로그에서 확인)
            first_col_empty = 0 not in dn_cols
            fewer = len(dn_cols) < len(up_cols)
            wrap = ((g[1] - g[0]) <= LINE_H_RATIO * line_h and dn_cols <= up_cols and only_spans
                    and first_col_empty and fewer)
        if not wrap:
            k += 1
        else:
            merged += 1
        rmap.append(k)
    if not merged:
        return 0
    data = _rebuild(cells, rmap, list(range(d.num_cols)), k + 1, d.num_cols)
    if data is None:
        return 0
    t.data = data
    return merged


def fix_table_spans(doc: DoclingDocument, lines: dict[int, PageLines] | None = None,
                    words: dict[int, list] | None = None) -> SpanLog:
    """문서의 모든 본문 표에서 놓친 병합 칸·쪼개지거나 합쳐진 열·줄바꿈 행을 바로잡는다 (문서를 직접 고친다).

    lines: 쪽 번호 → 선 (image_lines.read_lines). words: 쪽 번호 → 낱말 위치 (pdf_lines.read_pdf_words).
    """
    log = SpanLog()
    lines = lines or {}
    words = words or {}
    for t, _ in doc.iterate_items():
        if not isinstance(t, TableItem) or not t.prov or len(t.prov) != 1:
            continue
        pl = lines.get(t.prov[0].page_no)
        n_cols = _collapse_split_columns(doc, t, pl) if pl is not None else 0    # 쪼개진 열부터
        n_split = _split_fused_columns(doc, t, pl, words.get(t.prov[0].page_no)) if pl is not None else 0
        n_rows = _collapse_wrapped_rows(doc, t, pl) if pl is not None else 0     # 줄바꿈 행
        res = _fix_by_lines(doc, t, pl) if pl is not None else None
        # 선으로 판단 못 한 열(선이 없는 표 전체, 또는 안쪽 선이 안 잡힌 열)만 머리글 구조 규칙으로
        n_line, decided = res if res is not None else (0, set())
        n_head = _fix_header(t, decided)
        if n_line or n_head or n_cols or n_rows or n_split:
            log.tables.append({"page": t.prov[0].page_no, "size": f"{t.data.num_rows}x{t.data.num_cols}",
                               "cols_joined": n_cols, "cols_split": n_split, "rows_joined": n_rows,
                               "by_lines": n_line, "by_header": n_head})
    return log
