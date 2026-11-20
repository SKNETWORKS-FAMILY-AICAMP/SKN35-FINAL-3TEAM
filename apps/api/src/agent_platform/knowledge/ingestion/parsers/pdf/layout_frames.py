# -*- coding: utf-8 -*-
"""보이지 않는 틀 표 풀기 + 칸의 보이는 변 (kordoc table/layout-frames.ts · pdf/cell-edges.ts 이식).

이식 출처와 고지
  kordoc (https://github.com/chrisryugj/kordoc) — MIT License, Copyright 2026 chrisryugj.
  변경 사항: TypeScript → Python 이식. 칸 IR 대신 이 모듈의 Anchor를 쓴다. 분수 칸 합치기(mergeFractions), 목차 행 짝짓기(tocLines),
  칸 안 중첩 블록 재귀는 옮기지 않았다(우리 시험 문서에 드묾 — 필요해지면 옮긴다).

왜 필요한가
  한글 문서는 테두리를 없앤 표로 쪽을 짜는 일이 흔하다. 한컴 PDF는 테두리 '없음' 칸에도 칸 클립을 깔아, 클립 격자(clip_tables)는
  이 틀 표까지 표로 세운다. 원본 그림에 없는 격자다. 칸마다 실제로 그은 선(보이는 변)을 보고
    - 세로선이 보이는 행, 위아래 가로선이 온 폭에 그어진 사이에 칸이 둘 이상인 행 → 표 띠 (새 표로 다시 짠다)
    - 나머지 행 → 글 (표가 아니다)
  로 나눈다. 0단계 추적: kordoc이 이긴 표 중 9개가 이 단계를 거쳤다(주로 금감원 보도자료).

좌표: 보이는 변 판정은 PDF 원점(왼쪽 아래, 윗변 = y2). 한컴 PDF에서만 쓴다(다른 제작기도 글마다 클립을 깔 수 있음).
"""
from __future__ import annotations

from dataclasses import dataclass, field

EDGE_NEAR = 1.6
EDGE_COVER = 0.6
EDGE_SLACK = 3.0
DASH_GAP = 3.0
TIP_MAX = 1.5
SOLID_END_GAP = 2.0
SOLID_JOIN = 0.3


@dataclass
class Edges:
    t: bool
    b: bool
    l: bool
    r: bool


ALL_EDGES = Edges(True, True, True, True)


# ------------------------------------------------------------------ 칸의 보이는 변 (cell-edges.ts)
def _coverage(lines, horizontal: bool, at, a1, a2, near):
    spans = []
    for ln in lines:
        pos = ln.y1 if horizontal else ln.x1
        if abs(pos - at) > near:
            continue
        l1 = min(ln.x1, ln.x2) if horizontal else min(ln.y1, ln.y2)
        l2 = max(ln.x1, ln.x2) if horizontal else max(ln.y1, ln.y2)
        lo, hi = max(a1, l1), min(a2, l2)
        if hi <= lo or ((l1 < a1) != (l2 > a2) and hi - lo < TIP_MAX):
            continue                                       # 옆 칸 괘선의 끝 조각
        spans.append((lo, hi))
    if not spans:
        return 0.0, False, a2 - a1
    spans.sort()
    covered, (s, e), solid = 0.0, spans[0], True
    for lo, hi in spans[1:]:
        if lo > e + SOLID_JOIN:
            solid = False
        if lo <= e + DASH_GAP:
            e = max(e, hi)
        else:
            covered += e - s; s, e = lo, hi
    return covered + e - s, solid, max(spans[0][0] - a1, a2 - e)


def _seen(lines, horizontal, at, a1, a2, near=EDGE_NEAR) -> bool:
    ln = a2 - a1
    if ln <= 0:
        return False
    covered, solid, end_gap = _coverage(lines, horizontal, at, a1, a2, near)
    return covered >= ln * EDGE_COVER and ln - covered <= EDGE_SLACK and not (solid and end_gap > SOLID_END_GAP)


def cell_edges(box, hs, vs, h_near=EDGE_NEAR) -> Edges:
    """칸 사각형(x1, y1, x2, y2 — 왼쪽 아래 원점)의 보이는 변. hs·vs: 가로·세로 선(짧은 조각 포함)."""
    x1, y1, x2, y2 = box
    return Edges(t=_seen(hs, True, y2, x1, x2, h_near), b=_seen(hs, True, y1, x1, x2, h_near),
                 l=_seen(vs, False, x1, y1, y2), r=_seen(vs, False, x2, y1, y2))


# ------------------------------------------------------------------ 틀 풀기 (layout-frames.ts)
@dataclass
class Anchor:
    r: int
    c: int
    rs: int
    cs: int
    text: str
    edges: Edges | None = None
    box: tuple | None = None          # 왼쪽 아래 원점 (x1, y1, x2, y2) — 띠 표 위치 계산용
    content: bool = False             # 글 없이도 내용이 있는 칸(그림 칸) — 빈 칸으로 접지 않는다


@dataclass
class Band:
    rows: int
    cols: int
    cells: list[Anchor] = field(default_factory=list)   # r, c, rs, cs가 띠 표 기준으로 다시 매겨진 칸


def _rule_grids(rows, cols, anchors):
    H = [[False] * cols for _ in range(rows + 1)]
    V = [[False] * (cols + 1) for _ in range(rows)]
    for a in anchors:
        e = a.edges or ALL_EDGES                            # 모르면 보이는 칸 — 표를 지키는 쪽
        for c in range(a.c, a.c + a.cs):
            if e.t:
                H[a.r][c] = True
            if e.b:
                H[a.r + a.rs][c] = True
        for r in range(a.r, a.r + a.rs):
            if e.l:
                V[r][a.c] = True
            if e.r:
                V[r][a.c + a.cs] = True
    return H, V


def _ruled_rows(rows, cols, anchors, H, V):
    in_row = [0] * rows
    for a in anchors:
        for r in range(a.r, a.r + a.rs):
            in_row[r] += 1
    ruled = [any(V[r]) or (in_row[r] >= 2 and all(H[r]) and all(H[r + 1])) for r in range(rows)]
    crossed = [False] * (rows + 1)
    for a in anchors:
        for r in range(a.r + 1, a.r + a.rs):
            crossed[r] = True
    r0 = 0
    while r0 < rows:
        r1 = r0
        while r1 + 1 < rows and crossed[r1 + 1]:
            r1 += 1
        n = r1 - r0 + 1
        if n > 1 and all(H[r0]) and all(H[r1 + 1]) and sum(1 for a in anchors if a.r == r0 and a.rs == n) >= 2:
            for r in range(r0, r1 + 1):
                ruled[r] = True
        r0 = r1 + 1
    return ruled


def _blank(a: Anchor) -> bool:
    return not a.content and not a.text.strip()


def _band_table(cols, anchors, V, H, r0, r1, keep_empty_cols=False) -> Band | None:
    lst = [Anchor(a.r, a.c, min(a.rs, r1 - a.r + 1), a.cs, a.text, a.edges, a.box, a.content)
           for a in anchors if r0 <= a.r <= r1]
    lo, hi = float("inf"), float("-inf")
    for r in range(r0, r1 + 1):
        for c in range(cols + 1):
            if V[r][c]:
                lo, hi = min(lo, c), max(hi, c)
        for rr in (r, r + 1):
            for c in range(cols):
                if H[rr][c]:
                    lo, hi = min(lo, c), max(hi, c + 1)
    if lo != float("inf"):
        lst = [a for a in lst if (a.c < hi and a.c + a.cs > lo) or not _blank(a)]   # 선 밖 빈 들여쓰기 칸은 뺀다
    if not lst:
        return None
    # 선 없이 붙은 빈 여백 행은 접는다
    drop, corners = [], set()
    for r in range(r0, r1 + 1):
        starts = [a for a in lst if a.r == r]
        if not starts or (any(H[r]) and any(H[r + 1])) or not all(_blank(a) for a in starts):
            continue
        drop.append(r)
        for a in starts:
            e = a.edges
            if a.rs > 1 and e and (e.t or e.b) and (e.l or e.r):
                corners.add(id(a))
    if drop:
        def shift(y):
            return y - sum(1 for d in drop if d < y)
        kept = [a for a in lst if a.r not in drop]
        known_r = {v for a in kept for v in (shift(a.r), shift(a.r + a.rs))}
        known_c = {v for a in kept for v in (a.c, a.c + a.cs)}
        nl = []
        for a in lst:
            if a.r in drop and not (id(a) in corners and a.cs == 1 and shift(a.r + a.rs) - shift(a.r) == 1
                                    and shift(a.r) in known_r and shift(a.r + a.rs) in known_r and a.c in known_c and a.c + a.cs in known_c):
                continue
            na = Anchor(shift(a.r), a.c, shift(a.r + a.rs) - shift(a.r), a.cs, a.text, a.edges, a.box, a.content)
            if na.rs > 0:
                nl.append(na)
        lst = nl
    # 오른쪽 끝의 빈 열은 자른다
    if not keep_empty_cols and lst:
        right = max(a.c + a.cs for a in lst)
        while right > 1:
            starts = [a for a in lst if a.c == right - 1]
            if not starts or not all(_blank(a) for a in starts) or len(starts) == len(lst):
                break
            lst = [a for a in lst if a.c != right - 1]
            for a in lst:
                if a.c + a.cs == right:
                    a.cs -= 1
            right -= 1
    col_b = sorted({v for a in lst for v in (a.c, a.c + a.cs)})
    row_b = sorted({v for a in lst for v in (a.r, a.r + a.rs)})
    if len(col_b) < 2 or len(row_b) < 2:
        return None
    out = []
    for a in lst:
        r, c = row_b.index(a.r), col_b.index(a.c)
        out.append(Anchor(r, c, row_b.index(a.r + a.rs) - r, col_b.index(a.c + a.cs) - c, a.text, a.edges, a.box, a.content))
    return Band(len(row_b) - 1, len(col_b) - 1, out)


def unframe_table(rows: int, cols: int, anchors: list[Anchor], keep_empty_cols=False):
    """표 하나 → None(그대로 둔다) 또는 [('table', Band) | ('text', 글)] (풀어 낸 결과).

    테두리 정보(edges)가 하나도 없으면 손대지 않는다."""
    if not any(a.edges is not None for a in anchors):
        return None
    H, V = _rule_grids(rows, cols, anchors)
    ruled = _ruled_rows(rows, cols, anchors, H, V)
    if all(ruled):
        whole = _band_table(cols, anchors, V, H, 0, rows - 1, keep_empty_cols)
        if whole is None or (whole.rows == rows and whole.cols == cols):
            return None
        return [("table", whole)]
    out = []
    r = 0
    while r < rows:
        if ruled[r]:
            r1 = r
            while r1 + 1 < rows and ruled[r1 + 1]:
                r1 += 1
            sub = _band_table(cols, anchors, V, H, r, r1, keep_empty_cols)
            if sub:
                out.append(("table", sub))
            r = r1 + 1
            continue
        parts = [a.text.strip() for a in sorted((a for a in anchors if a.r == r), key=lambda a: a.c) if a.text.strip()]
        if len(parts) == 1:
            out += [("text", ln.strip()) for ln in parts[0].split("\n") if ln.strip()]
        elif parts:
            out.append(("text", " ".join(" ".join(p.split()) for p in parts)))
        r += 1
    return out
