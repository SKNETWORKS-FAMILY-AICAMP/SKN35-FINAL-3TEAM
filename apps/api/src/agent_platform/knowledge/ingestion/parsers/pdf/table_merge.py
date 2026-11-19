# -*- coding: utf-8 -*-
"""쪽 넘김으로 잘린 표 합치기 (Docling 결과 후처리).

왜 필요한가
  Docling은 쪽 단위로 표를 찾는다. 그래서 한 표가 다음 쪽으로 넘어가면 표 2~3개로 나뉜다.
  dev 진단(표 45개)에서 18개가 이 문제였다. 레이아웃·표 모델은 그대로 두고 결과 문서만 고친다.

무엇을 합치나 (모두 만족해야 함 — 잘못 합치는 것보다 안 합치는 쪽을 택한다)
  1. 두 표 사이 자리(앞 표 아래 ~ 뒤 표 위)에 본문 항목이 없다 (머리말·꼬리말은 무시).
  2. 앞 표는 p쪽, 뒤 표는 p+1쪽에 있다.
  3. 앞 표는 쪽 아래쪽(높이 80% 아래)에서 끝나고, 뒤 표는 쪽 위쪽(높이 20% 안)에서 시작한다.
  4. 두 표의 가로 범위가 많이 겹친다 (좁은 쪽 폭의 50% 이상).
  5. 열을 맞출 수 있다: 열 수가 같으면 순서대로, 다르면 칸 글자 위치(x)로 짝을 짓되
     한 열에 두 열이 겹치거나 순서가 꼬이면 합치지 않는다.
  6. 뒤 표에 캡션이 없다 (캡션이 있으면 새 표일 가능성이 크다).

합칠 때 하는 일
  - 머리글 반복: 뒤 표의 첫 행(최대 3행)이 앞 표의 첫 행과 글자가 같으면 버린다.
  - 행이 쪽에서 잘린 경우: 앞 표에 '번호 열'(머리글 아래 행의 80% 이상이 채워져 있고, 채워진 값이 모두 다른 열)이
    있는데 뒤 표 첫 행의 그 열이 비어 있으면, 그 행은 앞 표 마지막 행의 이어진 글자로 보고 칸마다 붙인다.
    (80%: 2줄 머리글의 아래 줄처럼 번호 칸이 빈 행이 있어도 번호 열로 인정하려고. 스캔 S2에서 확인.)
  - 세로 병합이 쪽을 넘는 경우: 앞 표 마지막 행을 덮는 세로 병합 칸(2행 이상)이 있고, 뒤 표 위쪽 몇 행의
    같은 열이 비어 있으면, 그 칸이 아래로 이어진 것으로 보고 병합 칸을 늘린다.
    (Word는 쪽 위에 테두리를 다시 그리므로 선으로는 알 수 없다 → 표 구조로 판단한다.)
  - 뒤 표의 쪽 위치(prov)를 앞 표에 붙이고, 뒤 표는 문서에서 지운다.

열 맞추기 (열 수가 다를 때)
  - 두 표 모두 PDF 선이 있으면: 표 영역의 세로선을 열 경계로 쓰고, 열마다 '글자 왼쪽 바로 앞 세로선'을
    그 열의 시작 x로 둔다. 좁은 표의 열은 넓은 표에서 그 시작 x를 품은 열(시작 x ~ 다음 열 시작 x)에 넣는다.
    넓은 표의 두 열이 같은 시작 x를 가지면(표 모델이 한 열을 쪼갬) 맞추지 않는다.
    좁은 표의 한 칸이 넓은 표의 여러 열 위에 있고 좁은 표 쪽에 그 사이 선이 없으면 가로 병합 칸으로 넓힌다.
  - 선이 없으면(스캔): 칸 글자 x 범위가 가장 많이 겹치는 열로 맞춘다.

한계
  - 열을 맞출 수 없으면 합치지 않는다 (틀리게 합치는 것보다 낫다).
  - HWP는 쪽을 넘는 병합 칸 글자를 두 쪽에 나눠 찍는다(앞 '물 리' / 뒤 '적 보 안'). 두 조각이 한 칸인지
    다른 묶음인지 글자·선으로 가릴 수 없어서 잇지 않는다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from agent_platform.knowledge.ingestion.parsers.pdf.pdf_lines import PageLines
from docling_core.types.doc import (
    ContentLayer,
    DocItem,
    DoclingDocument,
    TableCell,
    TableData,
    TableItem,
)

BOTTOM_ZONE = 0.80        # 앞 표 끝이 쪽 높이의 80%보다 아래
TOP_ZONE = 0.20           # 뒤 표 시작이 쪽 높이의 20%보다 위
MIN_X_OVERLAP = 0.50      # 가로 범위 겹침 / 좁은 표 폭
MAX_HEADER_ROWS = 3       # 반복 머리글로 볼 최대 행 수
MIN_ROWS_FOR_ID_COL = 3   # 번호 열을 판단하려면 머리글 아래 행이 이만큼은 있어야
ID_FILL = 0.80            # 번호 열: 머리글 아래 행 중 이 비율 이상이 채워져 있어야
LINE_SNAP = 4.0           # 글자 왼쪽에서 이만큼 오른쪽까지의 세로선도 '앞 선'으로 본다 (pt)
                          # 표 모델 칸 bbox는 몇 pt 어긋난다 (신구조문 대조표: 글자 212, 선 214)
LINE_MIN_COVER = 0.10     # 표 높이의 이 비율 이상을 덮는 세로선만 열 경계로 (일부 행에만 있는 경계도 잡으려고 낮게)


@dataclass
class MergeLog:
    """합친 내역 (나중에 검토·디버그용)."""
    merged: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ 위치 도우미
def _box(doc: DoclingDocument, prov):
    """prov bbox를 왼쪽 위 원점으로 바꿔 돌려준다 (y가 아래로 커짐)."""
    h = doc.pages[prov.page_no].size.height
    return prov.bbox.to_top_left_origin(h), h


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", s or ""))


def _row_texts(data: TableData, r: int) -> list[str]:
    """r행의 칸 글자 (병합 칸은 grid가 같은 칸을 반복해 준다)."""
    return [_norm(c.text) for c in data.grid[r]] if r < data.num_rows else []


def _col_ranges(doc: DoclingDocument, t: TableItem) -> dict[int, tuple[float, float]]:
    """열마다 x 범위: 한 열짜리 칸들의 글자 bbox를 모아 가장 왼쪽~가장 오른쪽.

    칸 bbox는 글자에 딱 붙은 크기라서 열 '경계'가 아니라 '글자가 있는 범위'다.
    """
    h = doc.pages[t.prov[0].page_no].size.height
    rng: dict[int, list[float]] = {}
    for c in t.data.table_cells:
        if c.col_span != 1 or c.bbox is None or not c.text.strip():
            continue
        b = c.bbox if c.bbox.coord_origin.value == "TOPLEFT" else c.bbox.to_top_left_origin(h)
        lo, hi = rng.get(c.start_col_offset_idx, [b.l, b.r])
        rng[c.start_col_offset_idx] = [min(lo, b.l), max(hi, b.r)]
    return {k: (v[0], v[1]) for k, v in rng.items()}


def _map_columns(narrow: dict, wide: dict, n_narrow: int, n_wide: int) -> list[int] | None:
    """좁은 표의 열 → 넓은 표의 열. 못 맞추면 None.

    점수 = 두 x 범위의 겹친 길이. 겹침이 없으면 떨어진 거리의 음수.
    같은 점수면 왼쪽 열을 고른다. 결과는 겹치지 않고(단사) 순서가 지켜져야(단조) 한다.
    """
    if n_narrow == n_wide:
        return list(range(n_narrow))
    if len(narrow) < n_narrow or len(wide) < n_wide:
        return None                                     # 위치 모르는 열이 있으면 포기
    out = []
    for i in range(n_narrow):
        a0, a1 = narrow[i]
        best, bs = None, None
        for j in range(n_wide):
            b0, b1 = wide[j]
            ov = min(a1, b1) - max(a0, b0)
            if bs is None or ov > bs:
                best, bs = j, ov
        out.append(best)
    if len(set(out)) != len(out) or out != sorted(out):
        return None
    return out


def _col_starts(doc: DoclingDocument, t: TableItem, prov, pl: PageLines | None):
    """(열마다 시작 x, 표 안 세로선, 표 오른쪽 끝). 선이 2개 미만이면 None(선 없음).

    열 시작 x = 그 열에서 시작하는 칸 글자의 왼쪽 바로 앞 세로선.

    칸마다 고른 선이 다르면 가장 많이 나온 선을 쓴다 (표 모델이 글자를 옆 열에 잘못 넣는 일이 있어서).
    """
    if pl is None:
        return None
    box, h = _box(doc, prov)
    xs = pl.verticals_in(box.l, box.t, box.r, box.b, min_cover=LINE_MIN_COVER)
    if len(xs) < 2:
        return None
    votes: dict[int, dict[float, int]] = {}
    for c in t.data.table_cells:
        if c.bbox is None or not c.text.strip():
            continue
        b = c.bbox if c.bbox.coord_origin.value == "TOPLEFT" else c.bbox.to_top_left_origin(h)
        left = [x for x in xs if x <= b.l + LINE_SNAP]
        if not left:
            continue
        v = votes.setdefault(c.start_col_offset_idx, {})
        v[left[-1]] = v.get(left[-1], 0) + 1
    starts = [max(votes[k], key=votes[k].get) if k in votes else None for k in range(t.data.num_cols)]
    # 글자가 하나도 없는 열(예: 전부 빈 '미흡' 열)은 시작 x를 모른다.
    # 앞뒤로 아는 열 사이에 있는 세로선 수가 모르는 열 수와 같으면 차례대로 채운다. 다르면 모르는 채로 둔다.
    k = 0
    while k < len(starts):
        if starts[k] is not None:
            k += 1
            continue
        e = k
        while e < len(starts) and starts[e] is None:
            e += 1
        lo = starts[k - 1] if k > 0 else -1e9
        hi = starts[e] if e < len(starts) else box.r - 2.0   # 마지막 열 뒤는 표 오른쪽 테두리
        cand = [x for x in xs if lo + 2.0 < x < hi - 2.0]
        if len(cand) == e - k:
            starts[k:e] = cand
        k = e
    return starts, xs, box.r


def _map_by_lines(narrow, wide: list) -> list[tuple[int, int]] | None:
    """선 기준 열 맞추기. 좁은 표 열 i → 넓은 표 열 범위 [s, e).

    s = 좁은 표 열의 시작 x를 품은 넓은 표의 열 (그 열 시작 x ~ 다음 열 시작 x).
    e: 좁은 표 열 i의 범위(시작 x ~ 다음 열 시작 x) 안에 넓은 표의 열이 더 있고,
       좁은 표의 쪽에서 그 사이에 세로선이 하나도 없으면 → 그 칸은 실제로 여러 열에 걸친 가로 병합이다.
       (HWP S2: 앞 쪽 머리글 '결과'는 아래 '양호·미흡' 두 열 위에 걸친 칸. 앞 쪽에는 그 사이 선이 없다.)
       선이 있으면 표 모델이 두 열을 한 칸으로 잘못 읽은 것이므로 넓히지 않는다.
    """
    nstarts, nxs, nright = narrow
    if any(x is None for x in nstarts) or any(x is None for x in wide):
        return None
    if any(b - a < 2.0 for a, b in zip(wide, wide[1:])):
        return None                                     # 넓은 표 열 시작이 겹치거나 꼬임 → 표 모델이 열을 잘못 쪼갬
    js = []
    for x in nstarts:
        j = max((k for k, w in enumerate(wide) if w <= x + 2.0), default=None)
        if j is None:
            return None
        js.append(j)
    if len(set(js)) != len(js) or js != sorted(js):
        return None
    out = []
    for i, (x, j) in enumerate(zip(nstarts, js)):
        nxt = nstarts[i + 1] if i + 1 < len(nstarts) else nright
        e = j + 1
        if not any(x + 2.0 < v < nxt - 2.0 for v in nxs):          # 좁은 표 쪽에 이 열 안쪽 선이 없음
            while e < len(wide) and wide[e] < nxt - 2.0:
                e += 1
        if i + 1 < len(js):
            e = min(e, js[i + 1])                                    # 다음 열과 겹치지 않게
        out.append((j, e))
    return out


# ------------------------------------------------------------------ 합칠 수 있는지
def _body_sequence(doc: DoclingDocument) -> list[DocItem]:
    """본문 항목을 읽기 순서로. 표의 자식(캡션 등)과 머리말·꼬리말은 뺀다."""
    seq = []
    for item, _ in doc.iterate_items():               # 기본값: 본문 층만
        if not isinstance(item, DocItem) or not item.prov:
            continue
        if item.content_layer != ContentLayer.BODY:
            continue
        parent = item.parent.resolve(doc) if item.parent else None
        if isinstance(parent, TableItem):
            continue
        seq.append(item)
    return seq


def _sits_between(doc: DoclingDocument, a: TableItem, b: TableItem, it: DocItem) -> bool:
    """읽기 순서상 두 표 사이에 온 항목이 실제로도 두 표 사이 자리에 있는가.

    앞 표 쪽에서 앞 표보다 아래, 또는 뒤 표 쪽에서 뒤 표보다 위에 있으면 '사이'다.
    Docling 읽기 순서가 가끔 앞 표 위쪽 문단을 표 뒤에 놓는데(S2 Word PDF에서 확인), 그런 항목은 무시한다.
    """
    ba, _ = _box(doc, a.prov[-1])
    bb, _ = _box(doc, b.prov[0])
    for p in it.prov:
        bi, _ = _box(doc, p)
        if p.page_no == a.prov[-1].page_no and bi.t >= ba.b - 2:
            return True
        if p.page_no == b.prov[0].page_no and bi.b <= bb.t + 2:
            return True
        if a.prov[-1].page_no < p.page_no < b.prov[0].page_no:
            return True
    return False


def _why_not(doc: DoclingDocument, a: TableItem, b: TableItem) -> str | None:
    """합치면 안 되는 이유. 합쳐도 되면 None."""
    if b.captions:
        return "뒤 표에 캡션"
    pa, pb = a.prov[-1], b.prov[0]
    if pb.page_no != pa.page_no + 1:
        return "이어진 쪽 아님"
    ba, ha = _box(doc, pa)
    bb, hb = _box(doc, pb)
    if ba.b < BOTTOM_ZONE * ha:
        return f"앞 표가 쪽 아래에서 안 끝남({ba.b / ha:.2f})"
    if bb.t > TOP_ZONE * hb:
        return f"뒤 표가 쪽 위에서 안 시작({bb.t / hb:.2f})"
    ov = min(ba.r, bb.r) - max(ba.l, bb.l)
    if ov < MIN_X_OVERLAP * min(ba.r - ba.l, bb.r - bb.l):
        return "가로 범위가 안 겹침"
    # 두 표 모두 첫 행이 머리글처럼 생겼는데 글자가 다르면 별개 표다.
    # (같으면 쪽마다 반복된 머리글 → 이어진 표). 시험셋 v2 로그: 쪽 끝·쪽 머리에 연달아 놓인 별개 표 두 개를 합친 일이 있었다.
    if _header_like(a.data) and _header_like(b.data) and _row_texts(a.data, 0) != _row_texts(b.data, 0):
        return "머리글이 다른 새 표"
    return None


HEAD_MAX_CHARS = 12       # 머리글 칸 글자 수 상한(공백 뺌)


def _header_like(data: TableData) -> bool:
    """첫 행이 머리글처럼 생겼나: 표가 2행 이상이고, 첫 행 칸 2개 이상이 모두 차 있고 짧으며(HEAD_MAX_CHARS 이하) 숫자가 없다.

    데이터 행은 보통 번호·수치가 있거나 빈 칸이 있어서 걸리지 않는다. 보수적으로 잡아 '확실한 머리글'만 머리글로 본다.
    """
    if data.num_rows < 2:
        return False                      # 머리글 아래 데이터 행이 있어야 '표의 머리글' (쪽에서 잘린 한 행 조각은 아니다)
    row = _row_texts(data, 0)
    uniq = [t for i, t in enumerate(row) if i == 0 or t != row[i - 1]]    # 가로 병합 칸은 grid에서 반복되므로 한 번만
    if len(uniq) < 2 or not all(uniq):
        return False
    return all(len(t) <= HEAD_MAX_CHARS and not any(ch.isdigit() for ch in t) for t in uniq)


# ------------------------------------------------------------------ 합치기
def _id_column(data: TableData) -> int | None:
    """번호 열: 첫 행(머리글) 아래 행의 ID_FILL 이상이 채워져 있고, 채워진 값이 모두 다른 열. 없으면 None.

    예전에는 '모든 행이 채워짐'을 요구했는데, 2줄 머리글의 아래 줄(번호 칸이 빈 행)이 있으면 번호 열을 못 찾아
    스캔 S2에서 쪽에서 잘린 행 조각이 새 행으로 들어갔다.
    마지막 행은 반드시 채워져 있어야 한다 → 그래야 '뒤 표 첫 행의 번호 칸이 비었음'이 '이어진 행'을 뜻한다.
    """
    if data.num_rows - 1 < MIN_ROWS_FOR_ID_COL:
        return None
    for c in range(data.num_cols):
        vals = [_norm(data.grid[r][c].text) for r in range(1, data.num_rows)]
        filled = [v for v in vals if v]
        if vals[-1] and len(filled) >= ID_FILL * len(vals) and len(set(filled)) == len(filled):
            return c
    return None


def _cell_at(cells: list[TableCell], r: int, c: int) -> TableCell | None:
    for x in cells:
        if x.start_row_offset_idx <= r < x.end_row_offset_idx and x.start_col_offset_idx <= c < x.end_col_offset_idx:
            return x
    return None


def _shift(c: TableCell, dr: int, cmap: list[tuple[int, int]] | None) -> TableCell:
    """칸을 dr행 아래로 옮기고, cmap(열 → [시작, 끝) 열)이 있으면 열 번호를 바꾼다."""
    s, e = c.start_col_offset_idx, c.end_col_offset_idx
    if cmap is not None:
        s, e = cmap[s][0], cmap[e - 1][1]
    return c.model_copy(update={
        "start_row_offset_idx": c.start_row_offset_idx + dr, "end_row_offset_idx": c.end_row_offset_idx + dr,
        "start_col_offset_idx": s, "end_col_offset_idx": e, "col_span": e - s,
    })


def _map_pair(doc: DoclingDocument, a: TableItem, b: TableItem, lines: dict | None):
    """(amap, bmap, 방법). 열이 적은 표를 많은 표에 맞춘다. 못 맞추면 None."""
    da, db = a.data, b.data
    if da.num_cols == db.num_cols:
        return None, None, "같은 열 수"
    lines = lines or {}
    sa = _col_starts(doc, a, a.prov[-1], lines.get(a.prov[-1].page_no))
    sb = _col_starts(doc, b, b.prov[0], lines.get(b.prov[0].page_no))
    if sa is not None and sb is not None:               # 두 표 모두 선이 있으면 선으로만 판단
        if db.num_cols < da.num_cols:
            m = _map_by_lines(sb, sa[0])
            return (None, m, "선") if m is not None else None
        m = _map_by_lines(sa, sb[0])
        return (m, None, "선") if m is not None else None
    ra, rb = _col_ranges(doc, a), _col_ranges(doc, b)   # 선이 없으면(스캔) 글자 위치로
    if db.num_cols < da.num_cols:
        m = _map_columns(rb, ra, db.num_cols, da.num_cols)
        return (None, [(j, j + 1) for j in m], "글자 위치") if m is not None else None
    m = _map_columns(ra, rb, da.num_cols, db.num_cols)
    return ([(j, j + 1) for j in m], None, "글자 위치") if m is not None else None


def _extend_vertical_spans(cells: list[TableCell], last: int, b_cells: list[TableCell]) -> list[int]:
    """쪽을 넘는 세로 병합 잇기. 늘린 병합 칸의 열 번호 목록을 돌려준다 (b_cells에서 이어진 빈 칸은 지운다).

    cells   : 앞 표 칸 (합친 열 번호)
    last    : 앞 표 마지막 행 번호
    b_cells : 뒤 표 칸 (이미 앞 표 아래로 옮긴 것)
    조건: 앞 표 마지막 행에서 끝나는 칸이 2행 이상 세로 병합이고, 뒤 표 맨 위부터 k행 동안 그 열(들)에 글자가 없다.
          2행 이상 병합만 보는 이유: '구분'처럼 묶음을 나타내는 열이라는 증거. 한 행짜리 칸 아래 빈 칸은
          그냥 빈 칸(예: 미흡 열이 비어 있음)일 수 있다.
    """
    done = []
    for host in cells:
        if host.row_span < 2 or host.end_row_offset_idx != last + 1:
            continue
        c0, c1 = host.start_col_offset_idx, host.end_col_offset_idx
        r, k = last + 1, 0
        while True:
            row_cells = [c for c in b_cells if c.start_row_offset_idx <= r < c.end_row_offset_idx]
            if not row_cells:
                break                                   # 뒤 표 끝
            here = [c for c in row_cells if c.start_col_offset_idx < c1 and c.end_col_offset_idx > c0]
            if any(c.text.strip() for c in here):
                break                                   # 그 열에 글자가 나오면 새 병합 칸의 시작
            if any(c.start_col_offset_idx < c0 or c.end_col_offset_idx > c1 or c.row_span > 1 for c in here):
                break                                   # 빈 칸 폭이 병합 칸과 다르면 판단하지 않는다
            k += 1
            r += 1
        if k == 0:
            continue
        b_cells[:] = [c for c in b_cells if not (last + 1 <= c.start_row_offset_idx < last + 1 + k
                                                  and c.start_col_offset_idx < c1 and c.end_col_offset_idx > c0)]
        host.end_row_offset_idx += k
        host.row_span += k
        done.append(c0)
    return done


def _merge_pair(doc: DoclingDocument, a: TableItem, b: TableItem, lines: dict | None = None) -> dict | None:
    """a 아래에 b를 붙인다. 성공하면 내역 dict, 열을 못 맞추면 None (문서는 그대로)."""
    da, db = a.data, b.data
    mp = _map_pair(doc, a, b, lines)
    if mp is None:
        return None
    amap, bmap, how = mp
    ncols = max(da.num_cols, db.num_cols)

    # 앞 표 칸 (필요하면 열 번호 바꿈)
    cells = [_shift(c, 0, amap) for c in da.table_cells]

    # 반복 머리글: 뒤 표 첫 k행이 앞 표 첫 k행과 같으면 버린다 (열 수가 같을 때만 비교 가능)
    drop = 0
    if da.num_cols == db.num_cols:
        for r in range(min(MAX_HEADER_ROWS, da.num_rows, db.num_rows)):
            if any(_row_texts(db, r)) and _row_texts(db, r) == _row_texts(da, r):
                drop = r + 1
            else:
                break

    # 행이 쪽에서 잘렸는지: 앞 표 번호 열이 뒤 표 첫 행(머리글 버린 다음)에서 비어 있으면 이어진 행
    cont = False
    idc = _id_column(da)
    if idc is not None and drop < db.num_rows:
        first = [c for c in db.table_cells if c.start_row_offset_idx == drop]
        target_cols = {(bmap[c.start_col_offset_idx][0] if bmap else c.start_col_offset_idx) for c in first if c.text.strip()}
        if target_cols and (amap[idc][0] if amap else idc) not in target_cols:
            cont = True
            last = da.num_rows - 1
            for c in first:
                if not c.text.strip():
                    continue
                col = bmap[c.start_col_offset_idx][0] if bmap else c.start_col_offset_idx
                host = _cell_at(cells, last, col)
                if host is not None:
                    host.text = (host.text + " " + c.text).strip()    # 잘린 글자 이어 붙이기
                else:
                    cells.append(TableCell(start_row_offset_idx=last, end_row_offset_idx=last + 1,
                                           start_col_offset_idx=col, end_col_offset_idx=col + 1,
                                           row_span=1, col_span=1, text=c.text))

    # 뒤 표 칸: 버린 행·이어 붙인 행을 빼고 앞 표 아래로 옮긴다
    skip = drop + (1 if cont else 0)
    dr = da.num_rows - skip
    moved = [_shift(c, dr, bmap) for c in db.table_cells if c.start_row_offset_idx >= skip]
    spans = _extend_vertical_spans(cells, da.num_rows - 1, moved)
    cells += moved
    nrows = da.num_rows + db.num_rows - skip

    a.data = TableData(num_rows=nrows, num_cols=ncols, table_cells=cells)
    a.prov = list(a.prov) + list(b.prov)
    doc.delete_items(node_items=[b])
    return {"pages": [a.prov[0].page_no, b.prov[0].page_no], "rows": nrows, "cols": ncols,
            "header_rows_dropped": drop, "row_continued": cont, "col_map": how, "spans_extended": spans}


def merge_split_tables(doc: DoclingDocument, lines: dict[int, PageLines] | None = None) -> MergeLog:
    """문서 안에서 쪽 넘김으로 잘린 표를 모두 합친다 (문서를 직접 고친다).

    lines: 쪽 번호 → PDF 선 (pdf_lines.read_pdf_lines). 없으면 글자 위치로만 열을 맞춘다.

    3쪽 이상 이어진 표도 합친다: 합친 결과를 다시 '앞 표'로 두고 다음 표와 비교한다.
    """
    log = MergeLog()
    changed = True
    while changed:                                      # 하나 합치면 순서가 바뀌므로 처음부터 다시 본다
        changed = False
        seq = _body_sequence(doc)
        for i, x in enumerate(seq):
            if not isinstance(x, TableItem):
                continue
            # 읽기 순서상 다음 표를 찾는다. 사이 항목이 '두 표 사이 자리'에 있으면 짝이 아니다.
            j = next((k for k in range(i + 1, len(seq)) if isinstance(seq[k], TableItem)), None)
            if j is None:
                continue
            y = seq[j]
            if any(_sits_between(doc, x, y, it) for it in seq[i + 1:j]):
                continue
            why = _why_not(doc, x, y)
            if why is None:
                info = _merge_pair(doc, x, y, lines)
                if info is not None:
                    log.merged.append(info)
                    changed = True
                    break
                why = "열을 맞출 수 없음"
            rec = {"pages": [x.prov[-1].page_no, y.prov[0].page_no], "why": why}
            if rec not in log.refused:
                log.refused.append(rec)
    return log
