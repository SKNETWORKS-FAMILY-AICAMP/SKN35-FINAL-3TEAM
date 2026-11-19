# -*- coding: utf-8 -*-
"""한컴 PDF 칸 클립 → 표 격자 (kordoc clip-cells.ts 이식).

이식 출처와 고지
  kordoc (https://github.com/chrisryugj/kordoc) src/pdf/clip-cells.ts — MIT License, Copyright 2026 chrisryugj.
  kordoc의 클립·선 기반 표 감지는 OpenDataLoader PDF(Hancom, Inc., Apache License 2.0)에서 유래한다.
  변경 사항: TypeScript → Python 이식. 클립·괘선·채움 수집은 pypdf로 새로 작성(pdf_graphics.py).
  판정 상수·순서는 원본을 따랐다. 원본 주석의 실측 근거는 줄여 옮겼다.

왜 필요한가 (계획_결과/kordoc_이식_0단계.md)
  한컴 PDF는 표 칸마다 잘라내기(clip) 사각형을 깐다. 이 사각형은 원본 칸 경계 그대로라 병합 칸·보이지 않는 칸 쪼개기까지
  담는다. 선을 보고 칸을 추정하는 대신 이것으로 칸을 바로 세우면 원본(HWPX) 격자와 거의 같아진다. kordoc이 우리보다 표를
  잘 읽은 것은 거의 전부 이 경로였다(정답 표 44개: kordoc 24승 / 우리 3승).

좌표: PDF 사용자 공간(왼쪽 아래 원점). 행 경계 rowYs는 위→아래(내림차순), 열 경계 colXs는 왼→오른.
아직 옮기지 않은 것(다음 단계): 보이지 않는 틀 표 풀기(layout-frames), 쪽 넘김 클립 표 잇기(table-parts),
  칸 글자 세부 규칙(cell-text). 칸 글자는 우리 글자층 낱말을 칸 상자에 넣는다(fill.word_lines).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from agent_platform.knowledge.ingestion.parsers.pdf.pdf_graphics import PageGraphics, Rect, Seg

CLIP_EDGE_TOL = 1.5
CLIP_ADJ_GAP = 0.15
CLIP_MIN_GROUP = 2
CLIP_MAX_PAGE_FRAC = 0.75
STRADDLE_MIN = 0.1
TITLED_FRAME_MIN_TOP = 0.2
TITLED_FRAME_MIN_WIDTH = 0.6
HEADER_BAND = 0.08
CLIP_MIN_W = 4.0
NARROW_FILL_MIN_W = 1.0
CLIP_MIN_H = 2.0
STROKE_NEAR = 2.0
STROKE_COVER = 0.5
CLIP_MIN_INVISIBLE = 0.0
CLIP_COORD_TOL = 0.3
CONT_X_TOL = 0.1
CLIP_SPACING_MAX = 3.0
STACKED_TABLE_GAP = 2.6


@dataclass
class GridCell:
    row: int
    col: int
    rowspan: int
    colspan: int
    bbox: Rect
    filler: bool = False


@dataclass
class ClipGrid:
    rowYs: list[float]
    colXs: list[float]
    bbox: Rect
    cells: list[GridCell]
    clip_parent: Rect | None = None
    continues: Rect | None = None


@dataclass
class ClipPage:
    last_cells: list[Rect] = field(default_factory=list)
    clips: list[Rect] = field(default_factory=list)


def _ov(a1, a2, b1, b2):
    return min(a2, b2) - max(a1, b1)


def _al(u, v):
    return abs(u - v) <= CLIP_COORD_TOL


def _adjacent(a: Rect, b: Rect) -> bool:
    if (abs(a.x2 - b.x1) <= CLIP_ADJ_GAP or abs(b.x2 - a.x1) <= CLIP_ADJ_GAP) and _ov(a.y1, a.y2, b.y1, b.y2) > CLIP_EDGE_TOL \
            and (_al(a.y1, b.y1) or _al(a.y2, b.y2)):
        return True
    if (abs(a.y2 - b.y1) <= CLIP_ADJ_GAP or abs(b.y2 - a.y1) <= CLIP_ADJ_GAP) and _ov(a.x1, a.x2, b.x1, b.x2) > CLIP_EDGE_TOL \
            and (_al(a.x1, b.x1) or _al(a.x2, b.x2)):
        return True
    return False


def _contains(a: Rect, b: Rect) -> bool:
    return (b.x1 >= a.x1 - CLIP_EDGE_TOL and b.x2 <= a.x2 + CLIP_EDGE_TOL and b.y1 >= a.y1 - CLIP_EDGE_TOL
            and b.y2 <= a.y2 + CLIP_EDGE_TOL
            and ((b.x2 - b.x1) < (a.x2 - a.x1) - CLIP_EDGE_TOL or (b.y2 - b.y1) < (a.y2 - a.y1) - CLIP_EDGE_TOL))


def _cluster(values):
    out, run = [], []
    for v in sorted(values):
        if run and v - run[-1] > CLIP_COORD_TOL:
            out.append(sum(run) / len(run)); run = []
        run.append(v)
    if run:
        out.append(sum(run) / len(run))
    return out


def _drop_sliver_gaps(coords):
    out = list(coords)
    if len(out) > 2 and out[1] - out[0] < 1:
        out[0:2] = [(out[0] + out[1]) / 2]
    n = len(out)
    if n > 2 and out[n - 1] - out[n - 2] < 1:
        out[n - 2:n] = [(out[n - 2] + out[n - 1]) / 2]
    return out


def _nearest(coords, v):
    best = 0
    for i in range(1, len(coords)):
        if abs(coords[i] - v) < abs(coords[best] - v):
            best = i
    return best


def _edge_stroked(lines: list[Seg], d: str, pos, a1, a2) -> bool:
    ln = a2 - a1
    if ln <= 0:
        return True
    for l in lines:
        if d == "h":
            if abs(l.y1 - pos) <= STROKE_NEAR and _ov(l.x1, l.x2, a1, a2) >= ln * STROKE_COVER:
                return True
        elif abs(l.x1 - pos) <= STROKE_NEAR and _ov(l.y1, l.y2, a1, a2) >= ln * STROKE_COVER:
            return True
    return False


# ------------------------------------------------------------------ 괘선 틈 (findRuledGaps · ruleEnd · closeGaps)
@dataclass
class _Gap:
    i: int
    j: int
    axis: str
    lo: float
    hi: float
    e1: float
    e2: float
    to: float
    both: bool


def _rule_end(lines, d, lo, hi, e1, e2):
    def at(p):
        return any(abs((l.y1 if d == "h" else l.x1) - p) <= 0.5
                   and (_ov(l.x1, l.x2, e1, e2) if d == "h" else _ov(l.y1, l.y2, e1, e2)) >= (e2 - e1) * STROKE_COVER for l in lines)
    a_lo, a_hi = at(lo), at(hi)
    to = lo if a_lo and not a_hi else hi if a_hi and not a_lo else (lo + hi) / 2
    return to, a_lo and a_hi


def _find_ruled_gaps(cells: list[Rect], parent: list[int], sh: list[Seg], sv: list[Seg]) -> list[_Gap]:
    def occupied(i, j, x1, y1, x2, y2):
        return any(n != i and n != j and parent[n] == parent[i] and _ov(k.x1, k.x2, x1, x2) > CLIP_ADJ_GAP
                   and _ov(k.y1, k.y2, y1, y2) > CLIP_ADJ_GAP for n, k in enumerate(cells))

    def gap_between(i, j):
        a, b = cells[i], cells[j]
        gv = a.y1 - b.y2
        if CLIP_ADJ_GAP < gv <= CLIP_SPACING_MAX and _ov(a.x1, a.x2, b.x1, b.x2) > CLIP_EDGE_TOL and (_al(a.x1, b.x1) or _al(a.x2, b.x2)) \
                and not occupied(i, j, max(a.x1, b.x1), b.y2, min(a.x2, b.x2), a.y1):
            e1, e2 = max(a.x1, b.x1), min(a.x2, b.x2)
            to, both = _rule_end(sh, "h", b.y2, a.y1, e1, e2)
            return _Gap(i, j, "y", b.y2, a.y1, e1, e2, to, both)
        gh = b.x1 - a.x2
        if CLIP_ADJ_GAP < gh <= CLIP_SPACING_MAX and _ov(a.y1, a.y2, b.y1, b.y2) > CLIP_EDGE_TOL and (_al(a.y1, b.y1) or _al(a.y2, b.y2)) \
                and not occupied(i, j, a.x2, max(a.y1, b.y1), b.x1, min(a.y2, b.y2)):
            e1, e2 = max(a.y1, b.y1), min(a.y2, b.y2)
            to, both = _rule_end(sv, "v", a.x2, b.x1, e1, e2)
            return _Gap(i, j, "x", a.x2, b.x1, e1, e2, to, both)
        return None

    def ruled(g: _Gap):
        lines = sh if g.axis == "y" else sv
        return any((abs((l.y1 if g.axis == "y" else l.x1) - g.lo) <= 0.5 or abs((l.y1 if g.axis == "y" else l.x1) - g.hi) <= 0.5)
                   and (_ov(l.x1, l.x2, g.e1, g.e2) if g.axis == "y" else _ov(l.y1, l.y2, g.e1, g.e2)) >= (g.e2 - g.e1) * STROKE_COVER
                   for l in lines)

    root = list(range(len(cells)))

    def find(i):
        while root[i] != i:
            root[i] = root[root[i]]; i = root[i]
        return i

    found: list[_Gap] = []
    widths: dict[int, list[tuple[str, float]]] = {}

    def link(g):
        found.append(g)
        ri, rj = find(g.i), find(g.j)
        w = widths.get(rj, []) + (widths.get(ri, []) if ri != rj else []) + [(g.axis, g.hi - g.lo)]
        root[ri] = rj
        widths[rj] = w

    cand = []
    for i in range(len(cells)):
        for j in range(len(cells)):
            if i == j or parent[i] != parent[j]:
                continue
            g = gap_between(i, j)
            if g is None:
                continue
            a, b = cells[i], cells[j]
            both = (_al(a.x1, b.x1) and _al(a.x2, b.x2)) if g.axis == "y" else (_al(a.y1, b.y1) and _al(a.y2, b.y2))
            r = ruled(g)
            if both and r:
                link(g)
            elif both or r:
                cand.append((g, both))
    if not found:
        return []
    grew = True
    while grew:
        grew = False
        for k in range(len(cand) - 1, -1, -1):
            g, both = cand[k]
            w = g.hi - g.lo
            if not any(any((both or s[0] == g.axis) and abs(s[1] - w) <= CLIP_COORD_TOL for s in widths.get(r, []))
                       for r in (find(g.i), find(g.j))):
                continue
            link(g)
            cand.pop(k)
            grew = True
    return found


def _close_gaps(members: list[Rect], gaps: list[_Gap]) -> list[Rect]:
    lines: list[_Gap] = []
    for g in gaps:
        l = next((k for k in lines if k.axis == g.axis and _al(k.lo, g.lo) and _al(k.hi, g.hi)), None)
        if l:
            l.e1, l.e2, l.both = min(l.e1, g.e1), max(l.e2, g.e2), l.both and g.both
        else:
            lines.append(_Gap(**vars(g)))
    spacing = any(g.axis == "x" for g in gaps) and any(g.axis == "y" for g in gaps)

    def repeated_blank_column(l):
        if spacing or l.axis != "x" or not l.both:
            return False
        spans = sorted((g.e1, g.e2) for g in gaps if g.axis == "x" and _al(g.lo, l.lo) and _al(g.hi, l.hi))
        if len(spans) < 3:
            return False
        covered, end, distinct = 0.0, -math.inf, 0
        for lo, hi in spans:
            if hi <= end + CLIP_COORD_TOL:
                continue
            covered += hi - max(lo, end); end = hi; distinct += 1
        height = max(r.y2 for r in members) - min(r.y1 for r in members)
        return distinct >= 3 and covered >= height * 0.65

    out = [Rect(r.x1, r.y1, r.x2, r.y2) for r in members]
    for l in lines:
        def shared(p):
            for u in members:
                for v in members:
                    if u is v:
                        continue
                    if l.axis == "y":
                        if abs(u.y1 - v.y2) <= CLIP_ADJ_GAP and _al(u.y1, p) and _ov(u.x1, u.x2, v.x1, v.x2) > CLIP_EDGE_TOL:
                            return True
                    elif abs(u.x2 - v.x1) <= CLIP_ADJ_GAP and _al(u.x2, p) and _ov(u.y1, u.y2, v.y1, v.y2) > CLIP_EDGE_TOL:
                        return True
            return False
        a_lo, a_hi = shared(l.lo), shared(l.hi)
        if repeated_blank_column(l) and not a_lo and not a_hi:
            continue
        to = l.lo if a_lo and not a_hi else l.hi if a_hi and not a_lo else l.to

        def on_edge(r):
            return (_al(r.y2, l.lo) or _al(r.y1, l.hi)) if l.axis == "y" else (_al(r.x2, l.lo) or _al(r.x1, l.hi))

        def span(r):
            return (r.x1, r.x2) if l.axis == "y" else (r.y1, r.y2)
        take: set[int] = set()
        grew = True
        while grew:
            grew = False
            for n, m in enumerate(members):
                if n in take or not on_edge(m):
                    continue
                a1, a2 = span(m)
                if _ov(a1, a2, l.e1, l.e2) <= -(CLIP_SPACING_MAX + CLIP_COORD_TOL):
                    continue
                take.add(n); l.e1 = min(l.e1, a1); l.e2 = max(l.e2, a2); grew = True
        for n in take:
            r, s = members[n], out[n]
            if l.axis == "y":
                if _al(r.y2, l.lo):
                    s.y2 = to
                if _al(r.y1, l.hi):
                    s.y1 = to
            else:
                if _al(r.x2, l.lo):
                    s.x2 = to
                if _al(r.x1, l.hi):
                    s.x1 = to
    return out


def _carried_band_top(colXs, top, sh, sv, text_points, cells, prev_last, head_y):
    x1, x2 = colXs[0], colXs[-1]
    if not any(_ov(b.x1, b.x2, x1, x2) > CLIP_EDGE_TOL for b in prev_last):
        return None
    y = None
    for l in sh:
        if l.y1 > top + CLIP_MIN_H and l.x1 <= x1 + STROKE_NEAR and l.x2 >= x2 - STROKE_NEAR and (y is None or l.y1 < y):
            y = l.y1
    if y is None or not _edge_stroked(sv, "v", x1, top, y) or not _edge_stroked(sv, "v", x2, top, y):
        return None
    above = max(y, head_y)
    if any(x1 < p[0] < x2 and top < p[1] < above for p in text_points):
        return None
    if any(_ov(c.x1, c.x2, x1, x2) > CLIP_EDGE_TOL and top - CLIP_EDGE_TOL <= c.y1 < above for c in cells):
        return None
    return y


def _add_narrow_edge_cols(colXs, rowYs, out: list[GridCell], fills: list[Rect]):
    if not fills:
        return

    def row_idx(y):
        i = _nearest(rowYs, y)
        return i if abs(rowYs[i] - y) <= CLIP_COORD_TOL else -1
    for side in ("l", "r"):
        edge = colXs[0] if side == "l" else colXs[-1]
        hits = []
        for f in fills:
            w = f.x2 - f.x1
            if w < NARROW_FILL_MIN_W or w >= CLIP_MIN_W or abs((f.x2 if side == "l" else f.x1) - edge) > CLIP_COORD_TOL:
                continue
            r0, r1 = row_idx(f.y2), row_idx(f.y1)
            if r0 >= 0 and r1 > r0:
                hits.append(f)
        if not hits:
            continue
        x = min(f.x1 for f in hits) if side == "l" else max(f.x2 for f in hits)
        if side == "l":
            colXs.insert(0, x)
            for c in out:
                c.col += 1
        else:
            colXs.append(x)
        col = 0 if side == "l" else len(colXs) - 2
        cx1, cx2 = colXs[col], colXs[col + 1]
        taken = [False] * (len(rowYs) - 1)
        for f in hits:
            r0, r1 = row_idx(f.y2), row_idx(f.y1)
            if any(taken[r0:r1]):
                continue
            for k in range(r0, r1):
                taken[k] = True
            out.append(GridCell(r0, col, r1 - r0, 1, Rect(cx1, rowYs[r1], cx2, rowYs[r0])))
        for r in range(len(taken)):
            if not taken[r]:
                out.append(GridCell(r, col, 1, 1, Rect(cx1, rowYs[r + 1], cx2, rowYs[r]), filler=True))


# ------------------------------------------------------------------ 본체 (buildClipCellGrids)
def build_clip_grids(g: PageGraphics, text_points: list[tuple[float, float]] = (),
                     prev: ClipPage | None = None) -> tuple[list[ClipGrid], list[Rect], ClipPage]:
    """한 쪽의 클립 사각형 → (격자들, 틀 사각형들, 다음 쪽에 넘길 사실)."""
    prev = prev or ClipPage()
    W, H = g.width, g.height
    page_area = W * H
    sh, sv, fills = g.horizontals, g.verticals, g.fills

    def same(a, b):
        return abs(a.x1 - b.x1) <= CLIP_EDGE_TOL and abs(a.x2 - b.x2) <= CLIP_EDGE_TOL and abs(a.y1 - b.y1) <= CLIP_EDGE_TOL \
            and abs(a.y2 - b.y2) <= CLIP_EDGE_TOL

    # 같은 사각형은 칸 안 문단마다 반복 클립된다 → 좌표로 중복 제거
    cells: list[Rect] = []
    buckets: dict[tuple[int, int], list[Rect]] = defaultdict(list)
    for r in g.clips:
        if (r.x2 - r.x1) < CLIP_MIN_W or (r.y2 - r.y1) < CLIP_MIN_H:
            continue
        if page_area > 0 and (r.x2 - r.x1) * (r.y2 - r.y1) >= page_area * CLIP_MAX_PAGE_FRAC:
            continue
        bx, by = math.floor(r.x1 / CLIP_EDGE_TOL), math.floor(r.y1 / CLIP_EDGE_TOL)
        if any(same(c, r) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for c in buckets.get((bx + dx, by + dy), [])):
            continue
        cells.append(r)
        buckets[(bx, by)].append(r)
    if not cells:
        return [], [], ClipPage()
    n = len(cells)

    def area(r):
        return (r.x2 - r.x1) * (r.y2 - r.y1)

    def straddles(a, b):
        ix, iy = _ov(a.x1, a.x2, b.x1, b.x2), _ov(a.y1, a.y2, b.y1, b.y2)
        if ix <= CLIP_EDGE_TOL or iy <= CLIP_EDGE_TOL or _contains(a, b) or _contains(b, a):
            return False
        inside = (ix * iy) / area(b)
        return STRADDLE_MIN < inside < 1 - STRADDLE_MIN

    # 포함 관계로 층을 나눈다 — 부모 = 자기를 품는 가장 작은 사각형 (걸쳐 겹치는 사각형은 부모가 못 된다)
    can_parent = [not any(i != j and straddles(a, b) for j, b in enumerate(cells)) for i, a in enumerate(cells)]
    parent = [-1] * n
    for i in range(n):
        for j in range(n):
            if i == j or not can_parent[j] or area(cells[j]) <= area(cells[i]) or not _contains(cells[j], cells[i]):
                continue
            cur = parent[i]
            if cur < 0 or _contains(cells[cur], cells[j]):
                parent[i] = j
    ruled_gaps = _find_ruled_gaps(cells, parent, sh, sv)
    is_container = [False] * n
    for p in parent:
        if p >= 0:
            is_container[p] = True
    # 표 겉 클립 — 자식 클립 묶음과 좌표까지 같으면 틀이 아니라 그 표의 겉 클립 (셀로 안 씀)
    tile_parent = [False] * n
    table_clip = [False] * n
    for p in range(n):
        if not is_container[p]:
            continue
        kids = [cells[i] for i in range(n) if parent[i] == p]
        if len(kids) < CLIP_MIN_GROUP:
            continue
        c = cells[p]
        x1, y1 = min(k.x1 for k in kids), min(k.y1 for k in kids)
        x2, y2 = max(k.x2 for k in kids), max(k.y2 for k in kids)
        if max(abs(x1 - c.x1), abs(x2 - c.x2), abs(y1 - c.y1), abs(y2 - c.y2)) <= CLIP_COORD_TOL:
            tile_parent[p] = True
    for p in range(n):
        if tile_parent[p]:
            table_clip[p] = not any(i != p and parent[i] == parent[p] and not tile_parent[i] and _adjacent(q, cells[p])
                                    for i, q in enumerate(cells))

    # 같은 층 변 공유 이웃끼리 묶음
    root = list(range(n))

    def find(i):
        while root[i] != i:
            root[i] = root[root[i]]; i = root[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            root[ra] = rb

    def edge_bin(v):
        return math.floor(v / CLIP_ADJ_GAP)
    starts_x, ends_x, starts_y, ends_y = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for i, c in enumerate(cells):
        starts_x[edge_bin(c.x1)].append(i); ends_x[edge_bin(c.x2)].append(i)
        starts_y[edge_bin(c.y1)].append(i); ends_y[edge_bin(c.y2)].append(i)
    for i, c in enumerate(cells):
        if table_clip[i]:
            continue
        cand = set()
        for mp, v in ((starts_x, c.x2), (ends_x, c.x1), (starts_y, c.y2), (ends_y, c.y1)):
            k = edge_bin(v)
            for d in (-1, 0, 1):
                cand.update(j for j in mp.get(k + d, []) if j > i)
        for j in sorted(cand):
            if table_clip[j] or parent[i] != parent[j]:
                continue
            if _adjacent(cells[i], cells[j]):
                union(i, j)
    # 괘선 틈을 사이에 둔 이웃도 한 표 — 단 따로 쌓은 두 표 사이의 넓은 가로 틈은 가른다
    stacked = set()
    if any(gg.axis == "y" and gg.both and gg.hi - gg.lo > STACKED_TABLE_GAP for gg in ruled_gaps):
        for i, c in enumerate(cells):
            if table_clip[i]:
                continue
            k = edge_bin(c.y2)
            for d in (-1, 0, 1):
                if any(j != i and not table_clip[j] and parent[j] == parent[i] and _adjacent(c, cells[j]) for j in starts_y.get(k + d, [])):
                    stacked.add(find(i))
    comp = [find(i) for i in range(n)]
    for gg in ruled_gaps:
        if table_clip[gg.i] or table_clip[gg.j]:
            continue
        if gg.axis == "y" and gg.both and gg.hi - gg.lo > STACKED_TABLE_GAP and comp[gg.i] != comp[gg.j] \
                and comp[gg.i] in stacked and comp[gg.j] in stacked:
            continue
        union(gg.i, gg.j)
    # 쪽 첫머리 띠의 떨어진 조각 (앞 쪽에서 쪼개져 넘어온 행)
    if prev.last_cells:
        head_top = H * (1 - HEADER_BAND)
        top = max((c.y2 for i, c in enumerate(cells) if parent[i] < 0 and not table_clip[i]), default=-math.inf)
        band = [i for i, c in enumerate(cells) if parent[i] < 0 and not table_clip[i] and abs(c.y2 - top) <= CLIP_COORD_TOL]
        y1b = cells[band[0]].y1 if band else 0
        if len(band) >= 2 and all(abs(cells[i].y1 - y1b) <= CLIP_COORD_TOL for i in band) \
                and not any(top + CLIP_EDGE_TOL < p[1] < head_top for p in text_points):
            for f, b in enumerate(cells):
                if parent[f] >= 0 or table_clip[f] or abs(b.y2 - y1b) > CLIP_ADJ_GAP:
                    continue
                on = [i for i in band if cells[i].x1 >= b.x1 - CLIP_COORD_TOL and cells[i].x2 <= b.x2 + CLIP_COORD_TOL]
                if len(on) < 2 or not any(_adjacent(cells[i], b) for i in on):
                    continue
                for i in on:
                    union(i, f)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    containers = [c for i, c in enumerate(cells) if is_container[i]]

    def is_grid_member(i):
        return len(groups[find(i)]) >= CLIP_MIN_GROUP

    def framed(r):
        return (_edge_stroked(sh, "h", r.y1, r.x1, r.x2) and _edge_stroked(sh, "h", r.y2, r.x1, r.x2)
                and _edge_stroked(sv, "v", r.x1, r.y1, r.y2) and _edge_stroked(sv, "v", r.x2, r.y1, r.y2))

    def titled_frame(r):
        return (r.y1 >= H * TITLED_FRAME_MIN_TOP and (r.x2 - r.x1) >= W * TITLED_FRAME_MIN_WIDTH
                and any(p[1] < r.y1 and p[1] > H * HEADER_BAND and r.x1 - CLIP_EDGE_TOL <= p[0] <= r.x2 + CLIP_EDGE_TOL for p in text_points)
                and any(r.x1 < p[0] < r.x2 and r.y1 < p[1] < r.y2 for p in text_points))

    def lone_frame(i):
        return is_container[i] and not is_grid_member(i) and (framed(cells[i]) or titled_frame(cells[i]))
    head_y = H * (1 - HEADER_BAND)

    def continues(i):
        if not prev.last_cells or parent[i] >= 0 or table_clip[i] or is_grid_member(i):
            return None
        c = cells[i]
        frm = next((b for b in prev.last_cells if abs(b.x1 - c.x1) <= CONT_X_TOL and abs(b.x2 - c.x2) <= CONT_X_TOL), None)
        if frm is None:
            return None
        if any(same(k, c) and not same(k, frm) for k in prev.clips):
            return None
        if any(c.y2 < p[1] < head_y for p in text_points):
            return None
        if any(o.y1 >= c.y2 - CLIP_EDGE_TOL and (o.y1 + o.y2) / 2 < head_y for o in cells):
            return None
        return frm

    def eff_parent(i):
        p = parent[i]
        steps = 0
        while p >= 0 and steps <= n:
            if not table_clip[p] and (is_grid_member(p) or lone_frame(p) or continues(p) is not None):
                return p
            p = parent[p]; steps += 1
        return -1

    def only_child(i):
        return parent[i] >= 0 and sum(1 for p in parent if p == parent[i]) == 1

    grids: list[ClipGrid] = []
    for idxs in groups.values():
        first = idxs[0]
        if table_clip[first]:
            continue
        ep = eff_parent(first)
        parent_rect = cells[ep] if ep >= 0 else None
        if len(idxs) < CLIP_MIN_GROUP:
            frm = continues(first)
            if frm is None and not lone_frame(first) and not ((parent_rect is not None or only_child(first)) and framed(cells[first])):
                continue
            r = cells[first]
            grids.append(ClipGrid([r.y2, r.y1], [r.x1, r.x2], Rect(r.x1, r.y1, r.x2, r.y2),
                                  [GridCell(0, 0, 1, 1, Rect(r.x1, r.y1, r.x2, r.y2))], parent_rect, frm))
            continue
        root_first = find(first)
        gs = [gg for gg in ruled_gaps if find(gg.i) == root_first]
        members = _close_gaps([cells[i] for i in idxs], gs) if gs else [cells[i] for i in idxs]
        edges = invisible = 0
        for r in members:
            edges += 4
            invisible += (not _edge_stroked(sh, "h", r.y1, r.x1, r.x2)) + (not _edge_stroked(sh, "h", r.y2, r.x1, r.x2)) \
                + (not _edge_stroked(sv, "v", r.x1, r.y1, r.y2)) + (not _edge_stroked(sv, "v", r.x2, r.y1, r.y2))
        if invisible / edges < CLIP_MIN_INVISIBLE:
            continue
        colXs = _drop_sliver_gaps(_cluster([v for r in members for v in (r.x1, r.x2)]))
        rowYs = list(reversed(_cluster([v for r in members for v in (r.y1, r.y2)])))     # 위→아래 내림차순
        band = None if parent_rect is not None else _carried_band_top(colXs, rowYs[0], sh, sv, text_points, cells, prev.last_cells, head_y)
        if band is not None:
            rowYs.insert(0, band)
        nr, nc = len(rowYs) - 1, len(colXs) - 1
        if nr < 1 or nc < 1:
            continue
        occupied = [[False] * nc for _ in range(nr)]
        out: list[GridCell] = []
        for r in members:
            c0, c1 = _nearest(colXs, r.x1), _nearest(colXs, r.x2)
            r0, r1 = _nearest(rowYs, r.y2), _nearest(rowYs, r.y1)
            if c1 <= c0 or r1 <= r0:
                continue
            if any(occupied[rr][cc] for rr in range(r0, r1) for cc in range(c0, c1)):
                continue                                     # 겹치는 클립(이중 그리기) — 먼저 온 칸 유지
            for rr in range(r0, r1):
                for cc in range(c0, c1):
                    occupied[rr][cc] = True
            out.append(GridCell(r0, c0, r1 - r0, c1 - c0, Rect(r.x1, r.y1, r.x2, r.y2)))
        for rr in range(nr):
            for cc in range(nc):
                if not occupied[rr][cc]:
                    out.append(GridCell(rr, cc, 1, 1, Rect(colXs[cc], rowYs[rr + 1], colXs[cc + 1], rowYs[rr]), filler=True))
        _add_narrow_edge_cols(colXs, rowYs, out, fills)
        grids.append(ClipGrid(rowYs, colXs, Rect(colXs[0], rowYs[-1], colXs[-1], rowYs[0]), out, parent_rect))
    # 이 쪽 마지막 내용인 최상위 칸 (다음 쪽 이어짐 판정용)
    tops = [c.bbox for gr in grids if gr.clip_parent is None for c in gr.cells if not c.filler]
    last: list[Rect] = []
    if tops:
        bottom = min(b.y1 for b in tops)
        foot_y = H * HEADER_BAND

        def below(y):
            return foot_y < y < bottom - CLIP_EDGE_TOL
        if not any(below(p[1]) for p in text_points) and not any(o.y2 < bottom and below((o.y1 + o.y2) / 2) for o in cells):
            last = [b for b in tops if abs(b.y1 - bottom) <= CLIP_COORD_TOL
                    and not any(abs(k.x1 - b.x1) <= CONT_X_TOL and abs(k.x2 - b.x2) <= CONT_X_TOL and k.y2 > b.y2 + CLIP_EDGE_TOL
                                and _contains(k, b) for k in cells)]
    return grids, containers, ClipPage(last, cells)


# ------------------------------------------------------------------ 문서 단위: 표 데이터로
def is_hancom_pdf(producer: str) -> bool:
    """한컴(HWP)이 만든 PDF인가 — 틀 풀기는 한컴 PDF에서만 한다 (kordoc parser.ts isHancomPdf)."""
    import re
    return any(re.match(r"\s*(?:hancom\b|hwp\b)", part, re.I) for part in producer.split(" | "))


def _table_data(nr, nc, anchors, H):
    from docling_core.types.doc import BoundingBox, CoordOrigin, TableCell, TableData
    cells = []
    for a in anchors:
        kw = {}
        if a.box is not None:
            x1, y1, x2, y2 = a.box
            kw["bbox"] = BoundingBox(l=x1, t=H - y2, r=x2, b=H - y1, coord_origin=CoordOrigin.TOPLEFT)
        cells.append(TableCell(start_row_offset_idx=a.r, end_row_offset_idx=a.r + a.rs, start_col_offset_idx=a.c,
                               end_col_offset_idx=a.c + a.cs, row_span=a.rs, col_span=a.cs, text=a.text, **kw))
    return TableData(num_rows=nr, num_cols=nc, table_cells=cells)


SPLIT_EDGE_TOL = 1.0   # 낱말 양끝에서 이만큼 안쪽에 칸 경계가 있어야 '칸에 걸친 낱말'


def _page_chars(pdf_path, pg, cache):
    """쪽의 pdfium 글자 상자 [(글자, l, t, r, b)] (왼쪽 위 원점, 공백 제외) — 칸에 걸친 낱말 나누기용, 문서마다 한 번 연다."""
    if "doc" not in cache:
        import pypdfium2 as pdfium
        cache["doc"] = pdfium.PdfDocument(str(pdf_path))
    if pg not in cache:
        page = cache["doc"][pg - 1]
        H = page.get_height()
        tp = page.get_textpage()
        n = tp.count_chars()
        text = tp.get_text_range(0, n)
        chars = []
        for k in range(min(n, len(text))):
            if text[k].isspace():
                continue
            try:
                l, b, r, t = tp.get_charbox(k)
            except Exception:
                continue
            chars.append((text[k], l, H - t, r, H - b))
        cache[pg] = chars
    return cache[pg]


def split_straddling_words(ws, grids, H, chars_of):
    """칸 경계를 가로지르는 낱말을 경계에서 나눈다 (kordoc cell-text.ts 칸에 걸친 글자 나누기의 낱말 판).

    글자층(docling_parse)은 칸 경계를 사이에 두고 가까이 붙은 글을 한 낱말로 잇는다(금감원 원화대출연체율: "변동" | "△0.09" 가
    4pt 틈으로 "변동△0.09" — 칸 가운데 기준으로 넣으면 "변동"이 옆 칸으로 간다). 그 자리 높이에서 실제 칸(클립) 변이 낱말 안쪽을
    지나면, pdfium 글자 위치로 낱말 글자를 칸마다 나눈다. pdfium 글자 수가 낱말 글자 수와 다르면 손대지 않는다."""
    edges_by_cell = [c.bbox for gr in grids if gr.clip_parent is None for c in gr.cells if not c.filler]
    out = []
    for w in ws:
        text, l, t, r, b = w[0], w[1], w[2], w[3], w[4]
        cy = H - (t + b) / 2
        edges = sorted({x for bb in edges_by_cell if bb.y1 <= cy <= bb.y2 for x in (bb.x1, bb.x2)
                        if l + SPLIT_EDGE_TOL < x < r - SPLIT_EDGE_TOL})
        if not edges:
            out.append(w)
            continue
        ch = sorted((c for c in chars_of() if l - 1 <= (c[1] + c[3]) / 2 <= r + 1 and t - 1 <= (c[2] + c[4]) / 2 <= b + 1),
                    key=lambda c: c[1])
        letters = [x for x in text if not x.isspace()]
        if len(ch) != len(letters):
            out.append(w)
            continue
        pieces: dict[int, list] = {}
        for x, c in zip(letters, ch):
            seg = sum(1 for e in edges if (c[1] + c[3]) / 2 > e)
            pieces.setdefault(seg, []).append((x, c))
        if len(pieces) < 2:
            out.append(w)
            continue
        for seg in sorted(pieces):
            p = pieces[seg]
            out.append(("".join(x for x, _ in p), min(c[1] for _, c in p), t, max(c[3] for _, c in p), b))
    return out


def clip_tables_for_pdf(pdf_path, words: dict[int, list] | None = None, unframe: bool = True, join: bool = True) -> list[dict]:
    """PDF → [{page, box(왼쪽 위 원점 l,t,r,b), data(TableData), continues(bool)}].

    words: {쪽: [(글자, l, t, r, b)]} 왼쪽 위 원점 (pdf_lines.read_pdf_words). 칸 글자를 채운다.
    최상위 격자(틀 안 중첩 아님)만 표로 낸다 — 채점·혼합은 최상위 표만 쓴다. 1×1 격자는 빼고 2칸 이상만.
    unframe: 한컴 PDF면 칸의 보이는 변으로 보이지 않는 틀 표를 푼다(layout_frames) — 표 띠만 표로 남는다.
    join: 쪽 넘김 클립 표 조각을 잇는다(table_parts) — 틀 풀기 전에 잇는다(kordoc 순서). 이은 표는 첫 쪽 자리에 두고 pages 에 쪽 목록.
    """
    from agent_platform.knowledge.ingestion.parsers.pdf.layout_frames import EDGE_NEAR, Anchor, cell_edges, unframe_table
    from agent_platform.knowledge.ingestion.parsers.pdf.pdf_graphics import read_page_graphics
    from agent_platform.knowledge.ingestion.parsers.pdf.table_backends.fill import word_lines

    from agent_platform.knowledge.ingestion.parsers.pdf.table_parts import PAGE_NUMBER_RE, Part, PCell, merge_parts, row_rules

    pages = read_page_graphics(pdf_path)
    char_cache: dict = {}
    out = []
    prev = ClipPage()
    parts: list[Part] = []
    hancom_doc = False
    for pg in sorted(pages):
        g = pages[pg]
        H = g.height
        hancom = unframe and is_hancom_pdf(g.producer)
        hancom_doc = hancom_doc or hancom
        hs, vs = g.horizontals + g.short_h, g.verticals + g.short_v
        ws = (words or {}).get(pg) or []
        pts = [((w[1] + w[3]) / 2, H - (w[2] + w[4]) / 2) for w in ws]     # 왼쪽 아래 원점으로
        grids, _, prev = build_clip_grids(g, pts, prev)
        if ws and grids:
            ws = split_straddling_words(ws, grids, H, lambda pg=pg: _page_chars(pdf_path, pg, char_cache))
        page_parts = []
        for gr in grids:
            nr, nc = len(gr.rowYs) - 1, len(gr.colXs) - 1
            if gr.clip_parent is not None or nr < 1:
                continue                                  # 최상위 격자만 (1×1 도 이어짐 조각일 수 있어 둔다)
            cells = []
            for c in gr.cells:
                l, t, r, b = c.bbox.x1, H - c.bbox.y2, c.bbox.x2, H - c.bbox.y1
                inside = [w for w in ws if l <= (w[1] + w[3]) / 2 <= r and t <= (w[2] + w[4]) / 2 <= b]
                wl = word_lines(inside)
                text = " ".join(" ".join(w[0] for w in ln) for ln in wl)
                # 칸 글줄 상자 (왼쪽 아래 원점): 바닥 y, 왼끝, 오른끝, 글자 높이 — 쪼개진 행 글 흐름 판정용
                lines = [(H - max(w[4] for w in ln), min(w[1] for w in ln), max(w[3] for w in ln), max(w[4] - w[2] for w in ln)) for ln in wl]
                ltext = [" ".join(w[0] for w in ln) for ln in wl]
                box = (c.bbox.x1, c.bbox.y1, c.bbox.x2, c.bbox.y2)
                edges = None
                if hancom:
                    near = min(EDGE_NEAR, (c.bbox.y2 - c.bbox.y1) / 3) if c.filler else EDGE_NEAR
                    edges = cell_edges(box, hs, vs, near)
                # 글 없는 메운 칸만 '클립 없던 자리' — 쪽 넘김 잇기에서 빈 자리로 본다
                cells.append(PCell(c.row, c.col, c.rowspan, c.colspan, text, edges, box, filler=c.filler and not text,
                                   lines=lines, ltext=ltext))
            cont = (gr.continues.x1, gr.continues.x2) if gr.continues is not None and nr * nc == 1 else None
            rules = row_rules([(c.bbox, c.filler) for c in gr.cells], gr.bbox, g.horizontals)
            page_parts.append(Part(pg, H, gr.bbox, list(gr.colXs), nr, nc, cells, rules, cont))
        parts += sorted(page_parts, key=lambda p: (-p.bbox.y2, p.bbox.x1))

    if join:
        def gap_free(pa, ya, pb, yb):
            """앞 쪽 ya 아래, 뒤 쪽 yb 위에 쪽 번호·머리말/꼬리말 띠(쪽 위아래 HEADER_BAND) 말고 글줄이 없나."""
            for pg, below, y in ((pa, True, ya), (pb, False, yb)):
                H = pages[pg].height
                ws = [w for w in (words or {}).get(pg) or []
                      if ((H - (w[2] + w[4]) / 2) < y - CLIP_EDGE_TOL if below else (H - (w[2] + w[4]) / 2) > y + CLIP_EDGE_TOL)]
                for ln in word_lines(ws):
                    cy = H - sum((w[2] + w[4]) / 2 for w in ln) / len(ln)
                    if cy < H * HEADER_BAND or cy > H * (1 - HEADER_BAND):
                        continue
                    if PAGE_NUMBER_RE.match(" ".join(w[0] for w in ln).strip()):
                        continue
                    return False
            return True
        parts = merge_parts(parts, gap_free)

    for p in parts:
        nr, nc, pg, H = p.rows, p.cols, p.page, p.page_h
        if nr * nc < 2:
            continue
        anchors = [Anchor(a.r, a.c, a.rs, a.cs, a.text, a.edges, a.box) for a in p.cells]
        hancom = unframe and hancom_doc
        res = unframe_table(nr, nc, anchors) if hancom else None
        joined = len(p.joined_pages) > 0
        if res is None:
            out.append({"page": pg, "box": (p.bbox.x1, H - p.bbox.y2, p.bbox.x2, H - p.bbox.y1),
                        "data": _table_data(nr, nc, anchors, H), "continues": False, "unframed": False,
                        "pages": [pg] + p.joined_pages})
            continue
        for kind, band in res:
            if kind != "table" or band.rows * band.cols < 2:
                continue
            bx = [a.box for a in band.cells if a.box]
            if bx:
                x1, y1 = min(b[0] for b in bx), min(b[1] for b in bx)
                x2, y2 = max(b[2] for b in bx), max(b[3] for b in bx)
            else:                                         # 뒤 쪽 칸만 든 띠 — 표 첫 쪽 상자로 둔다
                x1, y1, x2, y2 = p.bbox.x1, p.bbox.y1, p.bbox.x2, p.bbox.y2
            out.append({"page": pg, "box": (x1, H - y2, x2, H - y1), "data": _table_data(band.rows, band.cols, band.cells, H),
                        "continues": False, "unframed": True, "pages": [pg] + (p.joined_pages if joined else [])})
    if "doc" in char_cache:
        char_cache["doc"].close()
    return out
