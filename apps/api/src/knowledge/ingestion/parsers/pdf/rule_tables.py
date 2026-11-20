# -*- coding: utf-8 -*-
"""가로 규칙선 표 새로 찾기: 레이아웃 모델이 표로 못 찾은 '체크박스 서식표' 같은 표를 선으로 찾아 표로 넣는다.

왜 필요한가
  서울 결재문서의 '사전 검토항목 점검표'는 양옆 세로선이 없고, 행은 표 전체 너비의 가로 규칙선으로, 열은 일부 구간의
  세로선이나 가로선이 시작하는 자리(왼쪽 '구분' 열 끝)로만 나뉜다. 칸 안에는 □ ■ [ ] 같은 체크 칸이 많다.
  Docling 레이아웃 모델은 이 쪽을 '글자 + 체크박스' 항목 수십 개로 쪼개고 표는 0개로 낸다
  (시험셋 v2 공연예술제 18x5, v3 문화재단 26x18·정신의료기관 24x9 — v3 칸 관계의 약 20%가 여기서 빠졌다).
  선 격자(line_tables)는 Docling이 찾은 표만 바꾸고, 네 변·열 경계선이 다 그려진 격자만 받으므로 이 표를 못 살린다.

방법
  1) 표 자리: 쪽 너비의 H_MIN_FRAC 이상인 긴 가로선을 위→아래로 보며, 오른쪽 끝이 END_TOL 안에서 같고
     바로 위 선과 간격이 GAP_MAX 이하이면 같은 표로 잇는다. 서로 다른 y가 MIN_LINES 이상이면 표 후보.
  2) 행 경계: 표 자리 안 가로선 y. 열 경계 후보: 표 양끝 + 안쪽 세로선 x + 가로선 '끝점'(그 y에서 그 x를 가로지르는
     다른 가로선이 없을 때만 — 선이 그 자리에서 진짜로 끝난다는 뜻).
  3) 행(띠)마다 열 경계가 '있는지' 따로 판정한다: 세로선이 띠 높이의 V_COVER 이상 덮거나, 띠 위·아래 가로선이 그 x에서 끝나면 있음.
     표 양끝은 늘 있음(양옆이 트였어도). 없는 경계 양옆 칸은 가로로 합친다.
  4) 위아래 띠의 같은 열 범위 칸 사이에 가로선이 H_COVER 미만이면 세로로 합친다 (직사각형만 나온다).
  5) 칸 글자는 글자층 낱말로 채운다 (fill.word_lines, 선 격자와 같은 순서). 체크 칸 기호도 원문 그대로 남는다.
     한 칸의 글머리 항목(● ◆ ❍ …)은 행으로, '글머리 + 이름표 + 쌍점' 항목은 4칸으로, 체크 항목 나열은 항목마다 칸으로 나눈다
     (_table_data 참고).
  6) 넣기: 그 자리 안 본문 글자 항목(글자·체크박스·목록·소제목) 중 첫 항목 자리에 표를 넣고,
     글자가 이미 표에 들어간 항목(DUP_CHARS 이상)만 지운다.

안전장치
  - Docling 표와 겹치는 자리(표 자리 또는 Docling 표 넓이의 OVERLAP 이상)는 건드리지 않는다 (그건 line_tables 몫).
  - 그 자리 본문 글자 항목이 MIN_ITEMS 미만이면 표로 보지 않는다 (쪽 구분선·머리말 선 같은 장식선 걸러내기).
  - 띠의 MULTI_COL 이상이 칸 2개 이상으로 나뉘지 않으면 표가 아니다 (줄 친 글 상자, 보도자료 머리의 상자 더미).
  - 그 자리 글자 항목의 글자 중 KEEP_CHARS 이상이 새 표에 들어가야 넣는다.
  - 글자층이 없는 쪽(스캔)은 건드리지 않는다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
from knowledge.ingestion.parsers.pdf.table_backends.fill import word_lines
from docling_core.types.doc import (BoundingBox, CoordOrigin, DoclingDocument, ProvenanceItem, TableCell, TableData,
                                    TableItem, TextItem)

H_MIN_FRAC = 0.4    # 표 자리를 이루는 긴 가로선: 쪽 너비의 이 비율 이상
END_TOL = 6.0       # 오른쪽 끝·가로선 끝점이 이만큼(pt) 안이면 같은 자리
GAP_MAX = 150.0     # 이어진 두 긴 가로선 사이 최대 간격(pt) — 여러 줄 칸(정신의료기관 110pt) 허용
MIN_LINES = 4       # 서로 다른 y의 긴 가로선이 이 개수 이상 (= 3행 이상)
SNAP = 2.0          # 같은 경계로 볼 거리(pt)
MIN_CELL = 4.0      # 이보다 좁은 행·열은 이중선 틈으로 보고 합친다(pt)
V_COVER = 0.6       # 세로선이 띠 높이의 이 비율 이상이면 그 띠에 열 경계 있음
H_COVER = 0.6       # 칸 아래 가로선이 칸 너비의 이 비율 미만이면 위아래 칸을 합침
MIN_ITEMS = 12      # 그 자리 안 본문 글자 항목이 이 개수 이상이어야 표 후보 (대상 서식표 37~60개, 보도자료 머리 상자 더미 6개)
MULTI_COL = 0.5     # 행(띠)의 이 비율 이상이 칸 2개 이상으로 나뉘어야 표 (상자 더미는 띠마다 칸 하나)
OVERLAP = 0.3       # Docling 표와 이 비율 이상 겹치면 건드리지 않음
KEEP_CHARS = 0.9    # 그 자리 글자 항목 글자 중 이 비율 이상이 새 표에 있어야 넣음
DUP_CHARS = 0.8     # 항목 글자의 이 비율 이상이 표에 있으면 그 항목을 지움
BULLETS = ("●", "◆", "❍", "○", "◦", "‣", "•", "▶", "◇")   # 글머리 기호 (□ ■ 는 체크 칸이라 뺌)
LABEL_MAX = 12      # 글머리 뒤 쌍점 앞 이름표 최대 글자 수(공백 뺌) — 넘으면 문장 속 쌍점으로 본다
SPLIT_MIN = 2       # 같은 시작 열에 '글머리 + 이름표 + 쌍점' 항목이 이 개수 이상이어야 열을 나눈다


@dataclass
class RuleTableLog:
    added: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    items: list = field(default_factory=list, repr=False)   # 바꾸거나 넣은 표 객체 (참조 번호는 뒤 삭제로 바뀌므로 객체로 잡는다)


def _clusters(vals, tol=SNAP):
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _drop_thin(edges):
    out = [edges[0]]
    for e in edges[1:]:
        if e - out[-1] < MIN_CELL:
            out[-1] = (out[-1] + e) / 2
        else:
            out.append(e)
    return out


def find_regions(pl: PageLines, page_w: float) -> list[tuple]:
    """긴 가로선 연속 → 표 자리 [(l, t, r, b)]."""
    long = sorted((h for h in pl.h if h[2] - h[1] >= H_MIN_FRAC * page_w), key=lambda h: h[0])
    regions, cur = [], []
    for h in long:
        if cur and abs(h[2] - max(x[2] for x in cur)) <= END_TOL and h[0] - cur[-1][0] <= GAP_MAX:
            cur.append(h)
            continue
        if cur and abs(h[2] - max(x[2] for x in cur)) <= END_TOL and abs(h[0] - cur[-1][0]) <= SNAP:
            cur.append(h)                                  # 같은 y의 다른 조각
            continue
        if cur:
            regions.append(cur)
        cur = [h]
    if cur:
        regions.append(cur)
    out = []
    for g in regions:
        if len(_clusters([h[0] for h in g])) >= MIN_LINES:
            out.append((min(h[1] for h in g), min(h[0] for h in g), max(h[2] for h in g), max(h[0] for h in g)))
    return out


def _crosses(hs_at_y, x) -> bool:
    """그 y의 가로선 중 x를 가로지르는(양쪽으로 END_TOL 넘게 뻗은) 것이 있나."""
    return any(x0 < x - END_TOL and x1 > x + END_TOL for _, x0, x1 in hs_at_y)


def build_partial_grid(pl: PageLines, box):
    """표 자리 안 선 → (행 경계 ys, 열 경계 xs, 칸 [(r0, r1, c0, c1)]). 열이 하나뿐이면 None."""
    l, t, r, b = box
    hs = [h for h in pl.h if t - SNAP <= h[0] <= b + SNAP and min(h[2], r) - max(h[1], l) > 0]
    vs = [v for v in pl.v if l - SNAP <= v[0] <= r + SNAP and min(v[2], b) - max(v[1], t) > 0]
    ys = _drop_thin(_clusters([h[0] for h in hs]))
    by_y = {y: [h for h in hs if abs(h[0] - y) <= SNAP] for y in ys}
    cand = [x for x, _, _ in vs if l + MIN_CELL < x < r - MIN_CELL]
    for y, row in by_y.items():                           # 가로선이 진짜로 끝나는 자리 (다른 선이 그 자리를 안 지남)
        for _, x0, x1 in row:
            for x in (x0, x1):
                if l + MIN_CELL < x < r - MIN_CELL and not _crosses(row, x):
                    cand.append(x)
    xs = _drop_thin(_clusters([l, r] + cand))
    xs[0], xs[-1] = l, r
    nr, nc = len(ys) - 1, len(xs) - 1
    if nr < 2 or nc < 2:
        return None

    def present(c, r_):
        """띠 r_에 열 경계 xs[c]가 있나 (표 양끝은 늘 있음)."""
        if c in (0, nc):
            return True
        x, y0, y1 = xs[c], ys[r_], ys[r_ + 1]
        cov = sum(max(0.0, min(b1, y1) - max(b0, y0)) for a, b0, b1 in vs if abs(a - x) <= SNAP)
        if cov >= V_COVER * (y1 - y0):
            return True
        for y in (ys[r_], ys[r_ + 1]):                    # 띠 위·아래 가로선이 그 x에서 끝남
            row = by_y[y]
            if any(abs(x0 - x) <= END_TOL or abs(x1 - x) <= END_TOL for _, x0, x1 in row) and not _crosses(row, x):
                return True
        return False

    bands = []
    for r_ in range(nr):
        bounds = [c for c in range(nc + 1) if present(c, r_)]
        bands.append(list(zip(bounds, bounds[1:])))
    if sum(1 for bd in bands if len(bd) >= 2) < MULTI_COL * len(bands):
        return None                                        # 띠 대부분이 칸 하나 → 줄 친 글 상자·상자 더미 (금감원 보도자료 머리 상자)

    def h_cover(y, x0, x1):
        return sum(max(0.0, min(b1, x1) - max(a0, x0)) for _, a0, b1 in by_y[y]) / max(1e-6, x1 - x0)

    cells, open_ = [], {}
    for r_, bd in enumerate(bands):
        nxt = {}
        for c0, c1 in bd:
            k = open_.get((c0, c1))
            if k is not None and h_cover(ys[r_], xs[c0], xs[c1]) < H_COVER:
                cells[k][1] = r_ + 1                       # 위 칸과 세로로 합침
                nxt[(c0, c1)] = k
            else:
                cells.append([r_, r_ + 1, c0, c1])
                nxt[(c0, c1)] = len(cells) - 1
        open_ = nxt
    return ys, xs, [tuple(c) for c in cells]


def _items(lines: list[str]) -> list[str]:
    """칸 글자 줄들 → 글머리 항목들 (글머리로 시작하는 줄이 새 항목, 그 밖의 줄은 앞 항목에 이어 붙임).
    첫 줄이 글머리가 아니면 항목 없음([])."""
    if not lines or not lines[0].lstrip().startswith(BULLETS):
        return []
    out = []
    for ln in lines:
        if ln.lstrip().startswith(BULLETS):
            out.append(ln.strip())
        else:
            out[-1] += " " + ln.strip()
    return out


def _label_split(item: str):
    """'● 구 민 : 유 [ ] …' → ('●', '구 민', ':', '유 [ ] …'). 이름표(쌍점 앞)가 LABEL_MAX자(공백 뺌) 넘으면 None."""
    bullet, rest = item[0], item[1:].strip()
    k = min((rest.find(c) for c in (":", "：") if c in rest), default=-1)
    if k <= 0:
        return None
    label, value = rest[:k].strip(), rest[k + 1:].strip()
    if not label or len("".join(label.split())) > LABEL_MAX:
        return None
    return bullet, label, rest[k], value


# 체크 항목 나열('신규사업 □ 공약사업 □ …')을 줄마다 행·항목마다 칸으로 나누는 변형은 원본 작성자마다 습관이 달라
# v3 칸 F1 0.821→0.805 로 나빠져 뺐다 (2026-11-18). 그런 칸은 한 칸 그대로 둔다.


def _table_data(ys, xs, cells, words) -> TableData:
    """칸 글자를 채우고, 글머리 항목으로 칸을 나눈다 (논리 격자: 좌표 대신 행·열 번호를 늘린다).

    왜: 이 서식의 원본(HWPX)은 한 칸 안 글머리 항목('● 구민 : 유 [ ] ( ) 무 [√]' 5줄)을 행으로, 각 항목을
    '글머리 | 이름표 | : | 내용' 열로 나눠 그린다(정렬용 보이지 않는 칸). 선만 보면 한 칸이라 정답과 칸 관계가 크게 어긋났다
    (시험셋 v3 진단: 정신의료기관 19/128, 문화재단 26/260).
      - 행 나누기: 한 칸에 글머리 항목이 2개 이상이면 항목마다 행. 같은 띠의 다른 칸은 그만큼 세로로 늘린다(rowspan).
      - 열 나누기: '글머리 + 짧은 이름표 + 쌍점'인 항목이 같은 시작 열에 SPLIT_MIN개 이상이면 그 열을 4칸으로 늘리고
        그 항목들만 4칸에 나눠 넣는다. 그 열을 지나는 다른 칸은 가로로 늘린다(colspan).
      - 글머리 하나에 쌍점 없는 칸('◆ 제반 법규와 …?', 공연예술제)은 그대로 — 원본도 한 칸이다.
      - 시작 열마다 늘린 열 수는 그 열에서 가장 많이 나뉜 칸 기준. 덜 나뉜 칸은 마지막 조각이 남은 열까지 차지한다."""
    texts = {}
    for k, (r0, r1, c0, c1) in enumerate(cells):
        l, t, r, b = xs[c0], ys[r0], xs[c1], ys[r1]
        ws = [w for w in words if l <= (w[1] + w[3]) / 2 <= r and t <= (w[2] + w[4]) / 2 <= b]
        texts[k] = [" ".join(w[0] for w in ln) for ln in word_lines(ws)]
    items = {k: _items(v) for k, v in texts.items()}
    split_cols = Counter(c0 for k, (r0, r1, c0, c1) in enumerate(cells) for it in items[k] if _label_split(it))
    split_cols = {c for c, n in split_cols.items() if n >= SPLIT_MIN}
    # 칸마다 나눈 모양: [행마다 [조각 글자]] — 없으면 None(한 칸 그대로)
    shape = {}
    for k, (r0, r1, c0, c1) in enumerate(cells):
        its = items[k] if (r1 - r0 == 1 and len(items[k]) >= 2) or (c0 in split_cols and items[k]) else []
        if its:
            shape[k] = [list(_label_split(it)) if c0 in split_cols and _label_split(it) else [it] for it in its]
            continue
    # 띠마다 늘릴 행 수(한 띠짜리 칸만 행을 나눔), 시작 열마다 늘릴 열 수
    extra_rows, extra_cols = {}, {}
    for k, rows in shape.items():
        r0, r1, c0, c1 = cells[k]
        if r1 - r0 == 1 and len(rows) >= 2:
            extra_rows[r0] = max(extra_rows.get(r0, 0), len(rows) - 1)
        extra_cols[c0] = max(extra_cols.get(c0, 0), max(len(p) for p in rows) - 1)
    nr0, nc0 = len(ys) - 1, len(xs) - 1

    def fr(i):                                             # 원래 행 경계 i → 새 행 번호
        return i + sum(n for r, n in extra_rows.items() if r < i)

    def fc(j):                                             # 원래 열 경계 j → 새 열 번호
        return j + sum(n for c, n in extra_cols.items() if c < j)

    out = []

    def add(r0, r1, c0, c1, text, box):
        out.append(TableCell(start_row_offset_idx=r0, end_row_offset_idx=r1, start_col_offset_idx=c0, end_col_offset_idx=c1,
                             row_span=r1 - r0, col_span=c1 - c0, text=text,
                             bbox=BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin=CoordOrigin.TOPLEFT)))

    for k, (r0, r1, c0, c1) in enumerate(cells):
        box = (xs[c0], ys[r0], xs[c1], ys[r1])
        rows = shape.get(k)
        if not rows:
            add(fr(r0), fr(r1), fc(c0), fc(c1), " ".join(texts[k]), box)
            continue
        if r1 - r0 > 1 or len(rows) == 1:                 # 여러 띠에 걸친 칸은 행을 안 나누고 조각만 이어 붙인 한 행
            rows = [rows[0]] if len(rows) == 1 else [[" ".join(" ".join(p) for p in rows)]]
        for i, parts in enumerate(rows):
            ra = fr(r0) + i
            rb = fr(r1) if i == len(rows) - 1 else ra + 1           # 마지막 행이 남은 행까지 차지
            b0 = fc(c0)
            for j, txt in enumerate(parts[:-1]):
                add(ra, rb, b0 + j, b0 + j + 1, txt, box)
            add(ra, rb, b0 + len(parts) - 1, fc(c1), parts[-1], box)   # 마지막 조각이 남은 열까지 차지
    return TableData(num_rows=fr(nr0), num_cols=fc(nc0), table_cells=out)


def _chars(text: str) -> Counter:
    return Counter("".join(text.split()))


def _box(doc, prov):
    b = prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)
    return (b.l, b.t, b.r, b.b)


def _area(a):
    return max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])


def _inter(a, b):
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _top_level(doc: DoclingDocument, it):
    """본문(body) 바로 아래 조상 — 표를 목록 묶음 안이 아니라 본문 순서에 넣으려고."""
    while it.parent is not None:
        par = it.parent.resolve(doc)
        if par is doc.body:
            return it
        it = par
    return it


def apply_rule_tables(doc: DoclingDocument, lines: dict[int, PageLines], words: dict[int, list]) -> RuleTableLog:
    log = RuleTableLog()
    for page_no, pl in sorted(lines.items()):
        ws = words.get(page_no)
        if not ws or page_no not in doc.pages:
            continue
        W = doc.pages[page_no].size.width
        tables = [_box(doc, p) for t, _ in doc.iterate_items() if isinstance(t, TableItem) for p in t.prov if p.page_no == page_no]
        for box in find_regions(pl, W):
            if any(_inter(box, tb) >= OVERLAP * min(_area(box), _area(tb)) for tb in tables):
                continue                                   # Docling 표가 있는 자리 → line_tables 몫
            items = [it for it, _ in doc.iterate_items()
                     if isinstance(it, TextItem) and getattr(it, "prov", None) and it.prov[0].page_no == page_no
                     and (lambda b: box[0] <= (b[0] + b[2]) / 2 <= box[2] and box[1] <= (b[1] + b[3]) / 2 <= box[3])(_box(doc, it.prov[0]))]
            if len(items) < MIN_ITEMS:
                log.skipped.append({"page": page_no, "why": f"글자 항목 {len(items)}개(표로 보기엔 적음)"})
                continue
            g = build_partial_grid(pl, box)
            if g is None:
                log.skipped.append({"page": page_no, "why": "열이 하나뿐(줄 친 글 상자)"})
                continue
            data = _table_data(*g, ws)
            pool = _chars(" ".join(c.text for c in data.table_cells))
            before = _chars(" ".join(it.text for it in items))
            share = sum((before & pool).values()) / max(1, sum(before.values()))
            if share < KEEP_CHARS:
                log.skipped.append({"page": page_no, "why": f"글자 보존 안 됨({share:.2f})"})
                continue
            H = doc.pages[page_no].size.height
            nb = BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin=CoordOrigin.TOPLEFT)
            ref = items[0].prov[0].bbox.coord_origin
            prov = ProvenanceItem(page_no=page_no, charspan=(0, 0), bbox=nb if ref == CoordOrigin.TOPLEFT else nb.to_bottom_left_origin(H))
            new_t = doc.insert_table(sibling=_top_level(doc, items[0]), data=data, prov=prov, after=False)
            gone = [it for it in items if sum((_chars(it.text) & pool).values()) >= DUP_CHARS * max(1, sum(_chars(it.text).values()))]
            if gone:
                doc.delete_items(node_items=gone)
            tables.append(box)
            log.items.append(new_t)
            log.added.append({"page": page_no, "ref": new_t.self_ref, "size": f"{data.num_rows}x{data.num_cols}", "items": len(items),
                              "removed": len(gone), "kept_chars": round(share, 2)})
    return log
