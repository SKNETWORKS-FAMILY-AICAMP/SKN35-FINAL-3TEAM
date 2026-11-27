# -*- coding: utf-8 -*-
"""선 기반 표 만들기: 표 테두리 선으로 격자를 직접 만들고 칸에 글자를 넣는다 (표 모델 결과 대신).

왜 필요한가
  한국 공문서 표는 대부분 테두리가 그려져 있다. 그런데 표 구조 모델(TableFormer)은 영어 논문·재무 표 위주로 학습돼
  병합 많은 서식표·결재란·넓은 격자에서 행·열을 자주 틀린다(시험셋 v2: 기본형 표 구조 일치 15/62).
  모델 결과를 선으로 고치는 방식(table_spans)은 모델이 처음부터 틀리면 고칠 범위가 좁고 dev에 과적합됐다.
  선이 그려진 표는 선 자체가 정답 구조이므로, 선으로 바로 만든다.

방법
  1) 가로·세로 선을 서로 닿는 것끼리 묶어 '선 덩어리'를 만든다 → 가로선 2개·세로선 2개 이상이면 표 후보.
  2) 덩어리 안 가로선 y, 세로선 x를 SNAP pt 안으로 모아 행·열 경계로 쓴다 → 격자.
  3) 이웃 격자 칸 사이 경계에 선이 칸 길이의 COVER 이상 그려져 있지 않으면 같은 칸(병합)으로 잇는다.
     이은 결과가 직사각형이 아니면 그 표는 포기한다(모델 결과를 그대로 둔다).
  4) 낱말 위치(글자층)로 칸마다 글자를 넣는다 — 위→아래, 왼→오른 순서.
  5) 같은 쪽의 Docling 표와 자리가 겹치면(IoU ≥ MATCH_IOU) 그 표의 구조를 선 격자로 바꾼다.
  6) 표 위치를 선 격자 상자로 고치고, 새 표 자리 안의 본문 글자·그림 중 글자가 이미 표에 들어간 것은 지운다(중복).
  7) 예전 Docling 표 자리 안이었지만 선 격자 밖인 낱말(표 아래 서명란 등)은 표 앞·뒤 본문 글자로 되살린다.
  안전장치: 원래 표 글자의 KEEP_CHARS 이상이 바꾼 뒤에도 나오지 않으면 바꾸지 않는다.

하지 않는 일 (이번 단계)
  - Docling이 못 찾은 표를 새로 넣지 않는다(글자 항목 정리가 필요 — 다음 단계).
  - 선이 없거나 가로선만 있는 표, 낱말 위치가 없는 스캔은 건드리지 않는다.
  - 바깥 테두리가 덜 그려졌거나(네 변 각 80% 미만) 안쪽 열 경계 세로선이 표 높이의 절반에 못 미치는 표도 건드리지 않는다.
  - 선 묶음이 Docling 표보다 가로로 많이 좁은 표(양옆 열에 세로선이 없는 트인 표, WIDTH_COVER)도 건드리지 않는다.
    단 OPEN_EDGES 가 켜져 있으면 먼저 트인 변에 가상 테두리를 합성한다(open_edges) — 그러면 선 묶음이 표 폭이 되어 통과한다.
  - 가로선이 바깥 세로선 밖으로 튀어나간 표(한쪽만 트인 표, SIDE_TOL·OPEN_MIN)도 건드리지 않는다.
  - 격자 맨 위·맨 아래의 얇은 빈 행(쪽에서 잘린 행 조각, EDGE_ROW)은 버린다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from knowledge.ingestion.parsers.pdf.open_edges import close_open_edges
from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
from knowledge.ingestion.parsers.pdf.table_backends.fill import order_words, word_lines
from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, PictureItem, ProvenanceItem,
                                    TableCell, TableData, TableItem, TextItem)

SNAP = 2.0          # 이만큼 안의 선 위치는 같은 경계로 본다 (pt)
TOUCH = 3.0         # 선 끝이 이만큼 안이면 닿았다고 본다 (pt)
COVER = 0.6         # 경계 길이의 이 비율 이상 선이 있어야 '칸을 가르는 선'
MIN_CELL = 4.0      # 이보다 좁은 행·열은 이중선 틈으로 보고 합친다 (pt)
MATCH_IOU = 0.5     # Docling 표와 같은 표로 볼 겹침
BORDER = 0.8        # 바깥 테두리 네 변이 각각 이 비율 이상 그려져 있어야 '선이 다 그려진 표'
COL_LINE = 0.5      # 안쪽 열 경계마다 세로선이 표 높이의 이 비율 이상 있어야 한다
WIDTH_COVER = 0.9   # 선 묶음 너비가 Docling 표 너비의 이 비율 미만이면 '양옆이 트인 표'로 보고 손대지 않는다
SIDE_TOL = 6.0      # 가로선이 바깥 세로선보다 이만큼(pt) 넘게 나가면 '튀어나감'
OPEN_MIN = 2        # 튀어나간 가로선이 이 개수 이상이면 그쪽이 트인 표
EDGE_ROW = 20.0     # 맨 위·맨 아래 빈 행이 이보다 얇으면(pt) 쪽에서 잘린 행 조각으로 보고 버린다
KEEP_CHARS = 0.9   # 원래 표 글자의 이 비율 이상이 바꾼 뒤에도 나와야 바꾼다 (글자 보존 안전장치)
DUP_INSIDE = 0.5   # 본문 글자·그림 상자의 이 비율 이상이 새 표 자리 안이면 중복 후보
DUP_CHARS = 0.8     # 그 글자의 이 비율 이상이 표 칸 글자에 있으면 중복으로 보고 지운다
OPEN_EDGES = True   # 양옆이 트인 표에 가상 세로 테두리를 합성해 선 격자로 만든다 (open_edges — kordoc 이식)
VIRTUAL_IOU = 0.9   # 가상 테두리로 만든 격자는 Docling 표 자리와 이만큼 겹칠 때만 쓴다 — 위아래로 쌓인 두 표를 한 격자로
                    # 붙이는 일 방지 (v3 장애인시설 9x2 → 16x4 IoU 0.56, v2 식약처 16x6 → 18x6 IoU 0.86; 목표 표는 0.93 이상)
GREEDY = True       # 우리 격자가 포기한 표를 kordoc createMatrix 방식(build_grid_greedy)으로 다시 만든다
GREEDY_IOU = 0.9    # 그때도 Docling 표 자리와 이만큼 겹칠 때만 (가상 테두리와 같은 기준)
GREEDY_COVER = 0.75 # kordoc: 칸 경계 길이의 75% 이상을 선이 덮어야 칸을 가르는 선
GREEDY_TOL = 4.0    # kordoc: 선 위치 오차 max(4 × 선 굵기, 4) — 굵기 정보가 없어 4


@dataclass
class LineTableLog:
    replaced: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    items: list = field(default_factory=list, repr=False)   # 바꾸거나 넣은 표 객체 (참조 번호는 뒤 삭제로 바뀌므로 객체로 잡는다)


def _clusters(vals: list[float], tol: float) -> list[float]:
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _drop_thin(edges: list[float]) -> list[float]:
    """이중선 사이 아주 좁은 행·열을 없앤다."""
    out = [edges[0]]
    for e in edges[1:]:
        if e - out[-1] < MIN_CELL:
            out[-1] = (out[-1] + e) / 2
        else:
            out.append(e)
    return out


def _groups(pl: PageLines) -> list[tuple[list, list]]:
    """서로 닿는 가로·세로 선 묶음 [(가로선들, 세로선들)]."""
    hs, vs = list(pl.h), list(pl.v)
    n = len(hs) + len(vs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (y, x0, x1) in enumerate(hs):
        for j, (x, y0, y1) in enumerate(vs):
            if x0 - TOUCH <= x <= x1 + TOUCH and y0 - TOUCH <= y <= y1 + TOUCH:
                parent[find(i)] = find(len(hs) + j)
    out: dict[int, tuple[list, list]] = {}
    for i, h in enumerate(hs):
        out.setdefault(find(i), ([], []))[0].append(h)
    for j, v in enumerate(vs):
        out.setdefault(find(len(hs) + j), ([], []))[1].append(v)
    return [g for g in out.values() if len(g[0]) >= 2 and len(g[1]) >= 2]


def _covered(segs, pos, lo, hi, horizontal: bool) -> bool:
    """pos 위치(가로선이면 y, 세로선이면 x)에서 lo~hi 구간이 선으로 COVER 이상 덮였나."""
    tot = 0.0
    for a, b0, b1 in segs:
        if abs(a - pos) <= SNAP:
            tot += max(0.0, min(b1, hi) - max(b0, lo))
    return tot >= COVER * (hi - lo)


def build_grid(hs, vs):
    """선 묶음 → (행 경계 ys, 열 경계 xs, 칸 목록[(r0, r1, c0, c1)]). 직사각형이 안 되면 None."""
    ys = _drop_thin(_clusters([h[0] for h in hs], SNAP))
    xs = _drop_thin(_clusters([v[0] for v in vs], SNAP))
    nr, nc = len(ys) - 1, len(xs) - 1
    if nr < 1 or nc < 1 or (nr == 1 and nc == 1):
        return None
    # '선이 다 그려진 표'만 선으로 만든다. 양옆이 트인 표·열 경계를 일부 행에만 그은 표는 선이 없는 자리를
    # 병합으로 오판한다(시험셋 v2 진단: 금감원 통계표에서 7열이 5열로 합쳐짐) → 모델 결과를 그대로 둔다.
    W, Hh = xs[-1] - xs[0], ys[-1] - ys[0]
    for pos, lo, hi, segs in ((ys[0], xs[0], xs[-1], hs), (ys[-1], xs[0], xs[-1], hs)):
        if sum(max(0.0, min(b1, hi) - max(b0, lo)) for a, b0, b1 in segs if abs(a - pos) <= SNAP) < BORDER * W:
            return None
    for x in [xs[0], xs[-1]] + xs[1:-1]:
        need = BORDER if x in (xs[0], xs[-1]) else COL_LINE
        if sum(max(0.0, min(b1, ys[-1]) - max(b0, ys[0])) for a, b0, b1 in vs if abs(a - x) <= SNAP) < need * Hh:
            return None
    parent = {(r, c): (r, c) for r in range(nr) for c in range(nc)}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for r in range(nr):
        for c in range(nc):
            if c + 1 < nc and not _covered(vs, xs[c + 1], ys[r], ys[r + 1], False):
                parent[find((r, c + 1))] = find((r, c))
            if r + 1 < nr and not _covered(hs, ys[r + 1], xs[c], xs[c + 1], True):
                parent[find((r + 1, c))] = find((r, c))
    groups: dict = {}
    for k in parent:
        groups.setdefault(find(k), []).append(k)
    cells = []
    for members in groups.values():
        r0, r1 = min(m[0] for m in members), max(m[0] for m in members) + 1
        c0, c1 = min(m[1] for m in members), max(m[1] for m in members) + 1
        if len(members) != (r1 - r0) * (c1 - c0):
            return None                                  # ㄱ자 모양 칸 → 선을 잘못 읽었거나 복잡한 표: 포기
        cells.append((r0, r1, c0, c1))
    return ys, xs, sorted(cells)


def _greedy_vkind(vs, x, top, bot) -> int:
    """x 위치 세로선이 top~bot 구간을 덮나 — 0 없음, 1 선 하나가 GREEDY_COVER 이상, 2 끝이 맞닿은 토막 셋 이상의 합으로 덮음."""
    h = bot - top
    if h < 0.1:
        return 0
    spans = []
    for a, b0, b1 in vs:
        if abs(a - x) <= GREEDY_TOL:
            lo, hi = max(b0, top), min(b1, bot)
            if hi - lo >= GREEDY_COVER * h:
                return 1
            if hi > lo:
                spans.append((lo, hi))
    if len(spans) < 2:
        return 0
    spans.sort()
    best, (lo, hi), pieces = 0.0, spans[0], 1
    for a, b in spans[1:]:
        if a > hi + 0.5:
            if pieces >= 3:
                best = max(best, hi - lo)
            lo, hi, pieces = a, b, 1
        else:
            hi, pieces = max(hi, b), pieces + 1
    if pieces >= 3:
        best = max(best, hi - lo)
    return 2 if best >= GREEDY_COVER * h else 0


def build_grid_greedy(hs, vs):
    """kordoc cell-extract.ts extractCells(= ODL createMatrix) 이식 — 왼쪽 위 칸부터 훑으며, 오른쪽 경계에 세로선이 없으면
    옆으로, 아래 경계에 가로선이 없으면 아래로 늘린다. 늘 직사각형 칸이 나오고 바깥 테두리를 요구하지 않는다.
    (원본: kordoc MIT, ODL createMatrix Apache-2.0 Copyright 2025-2026 Hancom Inc. — 변경: 우리 선 형식, 좌표 왼쪽 위 원점)"""
    ys = _drop_thin(_clusters([h[0] for h in hs], SNAP))
    xs = _drop_thin(_clusters([v[0] for v in vs], SNAP))
    nr, nc = len(ys) - 1, len(xs) - 1
    if nr < 1 or nc < 1 or (nr == 1 and nc == 1):
        return None
    vk = [[_greedy_vkind(vs, xs[c], ys[r], ys[r + 1]) for c in range(nc + 1)] for r in range(nr)]

    def hline(y, l, r):
        w = r - l
        return w >= 0.1 and any(abs(a - y) <= GREEDY_TOL and min(b1, r) - max(b0, l) >= GREEDY_COVER * w for a, b0, b1 in hs)
    hb = [[hline(ys[r], xs[c], xs[c + 1]) for c in range(nc)] for r in range(nr + 1)]
    occ = [[False] * nc for _ in range(nr)]
    cells = []
    for r in range(nr):
        for c in range(nc):
            if occ[r][c]:
                continue
            cs = rs = 1
            while c + cs < nc and not any(vk[r + dr][c + cs] for dr in range(rs)):
                cs += 1
            while r + rs < nr:
                if any(hb[r + rs][c + dc] for dc in range(cs)):
                    break
                # 토막으로만 덮인 오른쪽 경계는 그 토막이 끊긴 아래 행까지 칸을 늘리지 않는다 (위계 들여쓰기 선)
                if c + cs < nc and vk[r][c + cs] == 2 and not vk[r + rs][c + cs]:
                    break
                rs += 1
            for dr in range(rs):
                for dc in range(cs):
                    occ[r + dr][c + dc] = True
            cells.append((r, r + rs, c, c + cs))
    return ys, xs, sorted(cells)


def _open_side(hs, gl: float, gr: float) -> str | None:
    """가로선 OPEN_MIN개 이상이 맨 바깥 세로선(gl·gr)보다 SIDE_TOL pt 넘게 밖으로 나가면 그쪽이 트였다 ('왼쪽'/'오른쪽')."""
    left = sum(1 for _, x0, _ in hs if x0 < gl - SIDE_TOL)
    right = sum(1 for _, _, x1 in hs if x1 > gr + SIDE_TOL)
    sides = [n for n, k in (("왼쪽", left), ("오른쪽", right)) if k >= OPEN_MIN]
    return "·".join(sides) or None


def _drop_empty_edge_rows(ys, filled):
    """맨 위·맨 아래 행이 모두 비었고 EDGE_ROW pt보다 얇으면 버린다 → (남길 행 시작, 끝(미포함)).

    쪽에서 잘린 행의 빈 조각: 앞 쪽 행이 다음 쪽으로 넘어오면 다음 쪽 표 맨 위에 얇은 빈 행이 선으로 그려진다.
    Docling은 무시하는데 선 격자는 행으로 센다 (시험셋 v3 진단: 사옥임대관리지침 4쪽 맨 위 13pt 빈 행 → 6x3이 7x3).
    filled: 글자가 있는 행 번호 집합(병합 칸은 걸친 행 모두)."""
    lo, hi = 0, len(ys) - 1
    if hi - lo > 1 and lo not in filled and ys[lo + 1] - ys[lo] < EDGE_ROW:
        lo += 1
    if hi - lo > 1 and (hi - 1) not in filled and ys[hi] - ys[hi - 1] < EDGE_ROW:
        hi -= 1
    return lo, hi


def grid_table(hs, vs, words, greedy=False) -> TableData | None:
    g = build_grid_greedy(hs, vs) if greedy else build_grid(hs, vs)
    if g is None:
        return None
    ys, xs, cells = g
    filled_cells = []
    for r0, r1, c0, c1 in cells:
        l, t, r, b = xs[c0], ys[r0], xs[c1], ys[r1]
        inside = order_words([w for w in words if l <= (w[1] + w[3]) / 2 <= r and t <= (w[2] + w[4]) / 2 <= b])
        filled_cells.append((r0, r1, c0, c1, " ".join(w[0] for w in inside)))
    filled = {r for r0, r1, _, _, txt in filled_cells if txt for r in range(r0, r1)}
    lo, hi = _drop_empty_edge_rows(ys, filled)
    out = []
    for r0, r1, c0, c1, text in filled_cells:
        r0, r1 = max(r0, lo), min(r1, hi)
        if r1 <= r0:
            continue                                     # 버린 빈 조각 행에만 있던 칸
        l, t, r, b = xs[c0], ys[r0], xs[c1], ys[r1]
        out.append(TableCell(start_row_offset_idx=r0 - lo, end_row_offset_idx=r1 - lo, start_col_offset_idx=c0, end_col_offset_idx=c1,
                             row_span=r1 - r0, col_span=c1 - c0, text=text,
                             bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT)))
    return TableData(num_rows=hi - lo, num_cols=len(xs) - 1, table_cells=out)


def _iou(a, b):
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _inside(a, box) -> float:
    """상자 a 넓이 중 box 안에 든 비율."""
    x0, y0, x1, y1 = max(a[0], box[0]), max(a[1], box[1]), min(a[2], box[2]), min(a[3], box[3])
    area = (a[2] - a[0]) * (a[3] - a[1])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0) / area if area > 0 else 0.0


def _char_share(text: str, pool: Counter) -> float:
    """text 글자(공백 뺌) 중 pool에 있는 비율."""
    c = Counter("".join(text.split()))
    n = sum(c.values())
    return sum((c & pool).values()) / n if n else 1.0


def _drop_duplicates(doc: DoclingDocument, table: TableItem, page_no: int, box) -> list[str]:
    """새 표 자리(box, 왼쪽 위 원점) 안의 본문 글자·그림 중 글자가 이미 표 칸에 있는 것을 지운다.

    왜: Docling이 표 일부(선 없는 정보 칸 등)를 그림·글자로 잡으면, 그 글자가 그림 글자 꺼내기(picture_text)로 본문에 나오고
    선 격자 표 안에도 들어가 두 번 나온다(시험셋 v2 기계설비 점검표: 정확도 0.894).
    지우는 조건: 상자의 DUP_INSIDE 이상이 표 자리 안 + 글자의 DUP_CHARS 이상이 표 칸 글자에 있음.
    그림은 자식 글자까지 함께 지운다(자식 글자가 없거나 모두 표 글자일 때만)."""
    pool = Counter("".join("".join(c.text.split()) for c in table.data.table_cells))
    H = doc.pages[page_no].size.height
    gone, dropped = [], []
    for it, _ in doc.iterate_items():
        if it is table or not getattr(it, "prov", None) or it.prov[0].page_no != page_no:
            continue
        if not isinstance(it, (TextItem, PictureItem)):
            continue
        if isinstance(it.parent.resolve(doc), (TableItem, PictureItem)) if it.parent else False:
            continue                                          # 표·그림 안 자식은 부모와 함께 처리
        b = it.prov[0].bbox.to_top_left_origin(H)
        if _inside((b.l, b.t, b.r, b.b), box) < DUP_INSIDE:
            continue
        if isinstance(it, PictureItem):
            kids = [c.resolve(doc) for c in it.children]
            text = " ".join(k.text for k in kids if isinstance(k, TextItem))
            # 글자 없는 그림은 진짜 그림이다(사진·그래프) — 지우지 않는다. 예전엔 빈 글자를 '모두 표 글자'로 보아 지웠다
            # (최종 비교 그림 채점에서 발견: v2 요양원 3쪽 사진이 선 격자 표와 겹쳐 사라짐)
            if not "".join(text.split()) or _char_share(text, pool) < DUP_CHARS:
                continue
            gone += kids + [it]
            dropped.append("그림")
        else:
            if _char_share(it.text, pool) < DUP_CHARS:
                continue
            gone.append(it)
            dropped.append(it.text[:20])
    if gone:
        doc.delete_items(node_items=gone)
    return dropped


def _restore_outside_words(doc: DoclingDocument, table: TableItem, page_no: int, old_box, new_box, words) -> list[str]:
    """예전 Docling 표 자리 안이었지만 새 선 격자 밖인 낱말을 본문 글자로 되살린다 (표 위쪽은 표 앞, 아래쪽은 표 뒤).

    왜: Docling 표 상자가 표 아래 글자(서명란 등)까지 잡은 경우, 예전엔 그 글자가 표 칸에 들어 있었다.
    선 격자는 선 테두리 안 낱말만 칸에 넣으므로 그 글자가 아무 데도 안 나왔다(dev 붙임4: 보존율 0.995 → 0.961).
    다른 본문 글자 항목 자리와 겹치는 낱말은 이미 나온 글자로 보고 넣지 않는다."""
    H = doc.pages[page_no].size.height
    others = []
    for it, _ in doc.iterate_items():
        if it is not table and isinstance(it, TextItem) and getattr(it, "prov", None) and it.prov[0].page_no == page_no:
            b = it.prov[0].bbox.to_top_left_origin(H)
            others.append((b.l, b.t, b.r, b.b))

    def center_in(w, box):
        cx, cy = (w[1] + w[3]) / 2, (w[2] + w[4]) / 2
        return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]

    out = [w for w in words if center_in(w, old_box) and not center_in(w, new_box)
           and not any(center_in(w, o) for o in others)]
    added = []
    for after, part in ((False, [w for w in out if (w[2] + w[4]) / 2 < new_box[1]]),
                        (True, [w for w in out if (w[2] + w[4]) / 2 > new_box[3]])):
        if not part:
            continue
        text = "\n".join(" ".join(w[0] for w in ln) for ln in word_lines(part))
        nb = BoundingBox(l=min(w[1] for w in part), t=min(w[2] for w in part), r=max(w[3] for w in part),
                         b=max(w[4] for w in part), coord_origin=CoordOrigin.TOPLEFT)
        prov = ProvenanceItem(page_no=page_no, charspan=(0, len(text)),
                              bbox=nb if table.prov[0].bbox.coord_origin == CoordOrigin.TOPLEFT else nb.to_bottom_left_origin(H))
        doc.insert_text(sibling=table, label=DocItemLabel.TEXT, text=text, prov=prov, after=after)
        added.append(text[:20])
    return added


def _kept_share(old: TableData, new: TableData, words, old_box, new_box) -> float:
    """원래 표 칸 글자(공백 뺌) 중 바꾼 뒤에도 나오는 비율 = 새 표 칸 글자 + 되살릴 낱말(예전 표 자리 안, 새 격자 밖)에 있는 것.

    왜: 낱말 위치가 선 격자 칸과 어긋나는 문서가 있다(시험셋 v3 사옥임대관리지침: 1·2쪽 표가 바뀐 뒤 칸 글자가 0 →
    보존율 0.80 → 0.60). 표 보정(table_spans)의 '글자 수 보존' 검사와 같은 종류의 안전장치다.
    원래 표 글자가 없으면(1.0) 막지 않는다."""
    before = Counter("".join("".join(c.text.split()) for c in old.table_cells))
    if not before:
        return 1.0

    def center_in(w, box):
        cx, cy = (w[1] + w[3]) / 2, (w[2] + w[4]) / 2
        return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]

    after = Counter("".join("".join(c.text.split()) for c in new.table_cells))
    after += Counter("".join("".join(w[0].split()) for w in words if center_in(w, old_box) and not center_in(w, new_box)))
    return sum((before & after).values()) / sum(before.values())


def apply_line_tables(doc: DoclingDocument, lines: dict[int, PageLines], words: dict[int, list],
                      relaxed: bool = True, only: set[int] | None = None, fallback: bool = False) -> LineTableLog:
    """글자층이 있는 쪽에서, Docling 표와 겹치는 선 격자 표가 있으면 표 구조를 선 격자로 바꾼다.

    relaxed: 트인 표 가상 테두리(OPEN_EDGES)와 늘려 만들기 격자(GREEDY)를 쓸지. 한컴 PDF 는 칸 클립(clip_tables)이 원본 칸을
      그대로 주므로 끈다 — 켜면 이 격자가 먼저 표를 바꿔 혼합 단계가 흔들렸다(v2 금감원 전자금융업 구조 일치 −1).
    only: 이 표 객체(id)들만 본다. None 이면 모든 표.
    fallback: 한컴 PDF 의 두 번째 호출 — 칸 클립 혼합이 끝난 뒤에도 표 모델(TableFormer) 그대로 남은 표(only)에만
      늘려 만들기 격자(GREEDY)를 시도한다. 가상 테두리는 쓰지 않고, 엄격 격자는 첫 호출에서 이미 해 봤으므로 건너뛴다.
      왜(2026-11-27 v4 재측정 C 진단): 규제영향분석서 16쪽 비용표 — Docling 이 붙은 두 표를 12x8 하나로 읽고 칸도 틀렸는데,
      칸 클립은 위 2x5 만 내 혼합에서 거절, 바깥 테두리가 덜 그려져 엄격 격자도 포기. 늘려 만들기 격자는 두 표를 정답 그대로 냈다
      (13/13, 21/21). 첫 호출에서 켜면 혼합이 흔들리므로(위 relaxed) 혼합 뒤 남은 표에만 쓴다."""
    log = LineTableLog()
    tables = [t for t, _ in doc.iterate_items() if isinstance(t, TableItem) and len(t.prov) == 1
              and (only is None or id(t) in only)]
    closed: dict[int, tuple] = {}                       # 쪽 → (가상 테두리를 더한 선, 가상 테두리 목록)
    for t in tables:
        p = t.prov[0]
        pl, ws = lines.get(p.page_no), words.get(p.page_no)
        if pl is None or not ws:
            continue
        if OPEN_EDGES and relaxed and not fallback:
            # 양옆이 트인 표에 가상 세로 테두리를 더한다 (kordoc closeOpenTableEdges 이식, open_edges) — 안쪽 세로선이
            # 가로 괘선 묶음과 실제로 교차할 때만 합성하므로, 선 없는 글 영역에는 생기지 않는다
            if p.page_no not in closed:
                virt = close_open_edges(pl.h, pl.v)
                closed[p.page_no] = (PageLines(pl.height, list(pl.h), list(pl.v) + virt), set(virt))
            pl, virt = closed[p.page_no]
        else:
            virt = set()
        bb = p.bbox.to_top_left_origin(doc.pages[p.page_no].size.height)
        tbox = (bb.l, bb.t, bb.r, bb.b)
        best, best_iou = None, 0.0
        for hs, vs in _groups(pl):
            gbox = (min(v[0] for v in vs), min(h[0] for h in hs), max(v[0] for v in vs), max(h[0] for h in hs))
            iou = _iou(tbox, gbox)
            if iou > best_iou:
                best, best_iou = (hs, vs), iou
        if best is None or best_iou < MATCH_IOU:
            continue
        if virt and any(v in virt for v in best[1]) and best_iou < VIRTUAL_IOU:
            log.skipped.append({"page": p.page_no, "why": f"가상 테두리 격자가 표 자리와 덜 겹침({best_iou:.2f})"})
            continue
        # 양옆이 트인 표: 맨 왼쪽·오른쪽 열에 세로선이 없으면 안쪽 세로선을 바깥 테두리로 잘못 보고 바깥 열을 버린다.
        # 이때 선 묶음은 Docling 표보다 가로로 좁다(시험셋 v2 진단: 금감원 통계표 0.77·0.83, 공연예술제 13x5 0.76 /
        # 잘 된 표는 약 1.0). 세로로만 다른 경우(Docling이 표 아래 글자까지 잡음, dev 붙임4)는 선 격자가 바로잡으므로 가로만 본다.
        gl, gr = min(v[0] for v in best[1]), max(v[0] for v in best[1])
        if gr - gl < WIDTH_COVER * (tbox[2] - tbox[0]):
            log.skipped.append({"page": p.page_no, "why": f"양옆이 트인 표(선 너비 {(gr - gl) / (tbox[2] - tbox[0]):.2f})"})
            continue
        # 한쪽만 트인 표: 가로선이 맨 바깥 세로선 밖으로 튀어나가면 그쪽에 세로선 없는 열이 있다.
        # 너비 비율(위)로는 못 잡는다 — 시험셋 v3 진단: 기술금융 평가결과 5x12, 왼쪽 '대형/소형' 열만 트여 너비 0.95 → 첫 열을 버림.
        side = _open_side(best[0], gl, gr)
        if side:
            log.skipped.append({"page": p.page_no, "why": f"한쪽이 트인 표({side})"})
            continue
        data = None if fallback else grid_table(best[0], best[1], ws)
        greedy = False
        if data is None and GREEDY and (relaxed or fallback) and best_iou >= GREEDY_IOU:
            # 우리 격자가 포기한 표(바깥 테두리 덜 그려짐·ㄱ자 병합)는 kordoc 방식(왼쪽 위부터 늘리기)으로 다시 만든다
            data, greedy = grid_table(best[0], best[1], ws, greedy=True), True
        if data is None:
            log.skipped.append({"page": p.page_no, "why": "격자가 직사각형 칸으로 안 나뉨"})
            continue
        gbox = (min(v[0] for v in best[1]), min(h[0] for h in best[0]), max(v[0] for v in best[1]), max(h[0] for h in best[0]))
        share = _kept_share(t.data, data, ws, tbox, gbox)
        if share < KEEP_CHARS:
            log.skipped.append({"page": p.page_no, "why": f"글자 보존 안 됨({share:.2f})"})
            continue
        before = f"{t.data.num_rows}x{t.data.num_cols}"
        t.data = data
        # 표 위치도 선 격자 상자로 (뒤 단계 — 쪽 넘김 합치기·그림 글자 판단 — 가 실제 표 자리를 쓰도록)
        H = doc.pages[p.page_no].size.height
        nb = BoundingBox(l=gbox[0], t=gbox[1], r=gbox[2], b=gbox[3], coord_origin=CoordOrigin.TOPLEFT)
        p.bbox = nb if p.bbox.coord_origin == CoordOrigin.TOPLEFT else nb.to_bottom_left_origin(H)
        dropped = _drop_duplicates(doc, t, p.page_no, gbox)
        restored = _restore_outside_words(doc, t, p.page_no, tbox, gbox, ws)
        used_virtual = any(v in virt for v in best[1])
        log.items.append(t)
        log.replaced.append({"page": p.page_no, "ref": t.self_ref, "before": before, "after": f"{data.num_rows}x{data.num_cols}",
                             "iou": round(best_iou, 2), **({"virtual_edges": True} if used_virtual else {}),
                             **({"greedy": True} if greedy else {}),
                             **({"dropped": dropped} if dropped else {}),
                             **({"restored": restored} if restored else {})})
    return log
