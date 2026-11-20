# -*- coding: utf-8 -*-
"""쪽 넘김 클립 표 조각 잇기 (kordoc pdf/table-parts.ts · pdf/cell-continuation.ts 일부 이식).

이식 출처와 고지
  kordoc (https://github.com/chrisryugj/kordoc) — MIT License, Copyright 2026 chrisryugj.
  쪽 걸친 표 병합의 뼈대는 Hancom OpenDataLoader PDF TableBorderProcessor.checkNeighborTables (Apache-2.0,
  Copyright 2025-2026 Hancom Inc.) 에서 왔다.
  변경 사항: TypeScript → Python 이식. 표 IR 대신 이 모듈의 Part/PCell 을 쓴다. 옮긴 것 —
    · 한 칸 이어짐 조각 붙이기 (mergeContinuedCells — 칸 안 중첩 표 재귀는 뺐다)
    · 쪽 넘김 판정 (looksContinued — 그림 칸 밀려남 pushedLead 는 뺐다), 새 표 시작(restartsTable·chainHead)
    · 클립 조각 잇기 (joinClipParts · joinSplitParts — 열 경계 합집합 격자, 반복 머리 행, 세로 병합 잇기)
    · 쪼개진 행 합치기 (mergeSplitRow) — 기하 증거(괘선 없는 경계·이어 늘린 빈 칸·띠)와 글 흐름 증거
      (continuesAcross·clauseContinues·quantityContinues·wordOverflows·listContinues·lineEnded·nextItemHead,
       split-row-pattern 의 hasNumberedRowSequence·matchesIndependentRowPair·matchesComplete{Context,Sibling,Rubric}Row).
      칸 글줄 위치는 kordoc 처럼 글자 조각 기준선이 아니라 낱말 상자 바닥(y)·높이(h)로 잰다.
  옮기지 않은 것: mergeStraddlingCells(쪽 경계에 걸친 세로 병합 칸 잇기), partialAlignedLabelCells·
    matchesCompleteAlignedLabelListRow(이름표:값 목록 칸), 같은 쪽 2단 넘김(mergeColumnFlow), 열 수 같은 일반 표 붙이기(선 표용).

왜 필요한가
  한컴 PDF는 쪽마다 그 쪽에 그려진 칸만 클립으로 깐다. 표가 쪽을 넘으면 조각마다 열 구성이 달라(세로 병합 칸이 다음 쪽에서
  비면 그 열 클립이 없다) 열 수만 보고 잇는 방식으로는 못 잇는다. 두 조각 열 경계의 합집합 격자에 칸을 다시 놓고, 앞 쪽에서
  넘어온 세로 병합 칸은 다음 쪽 빈 자리만큼 늘린다. 0단계 추적: 이 경로에서 kordoc 이 맞힌 관계가 835 대 595로 가장 컸다.

좌표: PDF 원점(왼쪽 아래, y 위로). bbox 는 Rect(x1, y1, x2, y2), y2 가 윗변.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from knowledge.ingestion.parsers.pdf.layout_frames import Edges
from knowledge.ingestion.parsers.pdf.pdf_graphics import Rect

PART_COL_TOL = 1.0              # 같은 짜임이 쪽마다 조금 어긋난 경계를 짝짓는 거리
PART_COL_SNAP = 0.3             # 짜임이 다를 때 같은 경계로 보는 클립 좌표 오차
CONTINUATION_COL_TOL = 2.0      # 쪽 넘김 이음 — 열 경계가 이 안에서 맞으면 같은 경계
FOREIGN_FIRST_ROW_CELLS = 3
PAGE_EDGE_BAND = 0.16           # 앞 조각 밑변이 쪽 아래 16% 안, 뒤 조각 윗변이 쪽 위 16% 안
RESTART_MIN_ANCHORS = 3
TITLE_ROW_MAX_CHARS = 40
RESTART_LOOKBACK_PAGES = 5
COL_MATCH_TOL = 0.5             # 한 칸 이어짐 조각의 좌우 변과 앞 표 열 경계
CARRIED_MIN_COL_W = 4.0
RULE_Y_TOL = 1.5                # 괘선이 칸 변에 붙었다고 보는 거리
NO_EDGES = Edges(False, False, False, False)

ANNEX_HEAD_RE = re.compile(r"^\s*[<\[(【]?\s*(?:붙\s*임|참\s*고|별\s*첨|별\s*지|별\s*표|첨\s*부|부\s*록)(?:\s*\d|\s*$|\s*[>\])】])|^\s*■")
PAGE_NUMBER_RE = re.compile(r"^[-–—]?\s*\d{1,4}\s*[-–—]?$")
UNIT_ROW_RE = re.compile(r"^\s*\(\s*단위\s*[:：]")
SERIAL_RE = re.compile(r"^\d{1,3}$")
CONTACT_HEAD = re.compile(r"^(?:담당|담당부서|담당자|문의|연락처)")
CONTACT_ROLE = re.compile(r"(?:책임자|담당자|과장|팀장|국장|사무관|주무관|연구관|연구사)")


# ------------------------------------------------------------------ 자료 모양
@dataclass
class PCell:
    """표 칸 (격자 자리 r, c 와 병합 rs, cs). filler: 클립 없이 비어 메운 자리 — 세로 병합 잇기가 채운다."""
    r: int
    c: int
    rs: int
    cs: int
    text: str = ""
    edges: Edges | None = None
    box: tuple | None = None          # 왼쪽 아래 원점 (x1, y1, x2, y2) — 표 첫 쪽 칸만 (다른 쪽 칸은 None)
    filler: bool = False
    lines: list = field(default_factory=list)   # 칸 글줄 [(y 바닥, l, r, h 글자 높이)] 위→아래 — 쪼개진 행 글 흐름 판정 (kordoc CELL_LINES)
    ltext: list = field(default_factory=list)   # 글줄마다 글 — 목록 번호·개조식 판정은 줄 단위로 본다


@dataclass
class RowRules:
    """표 가로 괘선 — 윗변·밑변이 그어졌는지, 칸 밑변(표 밑변 제외) 중 그어진/안 그어진 수 (kordoc recordRowRules)."""
    top: bool
    bottom: bool
    inner_ruled: int
    inner_open: int


@dataclass
class Part:
    page: int
    page_h: float
    bbox: Rect
    colXs: list[float]
    rows: int
    cols: int
    cells: list[PCell]
    rules: RowRules | None = None
    cont: tuple | None = None          # 앞 쪽 칸의 이어짐 1칸 조각이면 그 칸 좌우 (x1, x2)
    part_colXs: list[float] | None = None
    tail: tuple | None = None          # 마지막 행 칸이 다음 쪽으로 넘어가 붙인 조각의 (쪽, bbox)
    joined_pages: list[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(c.text.strip() for c in self.cells)


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def _anchors(t: Part) -> list[PCell]:
    """메운 자리까지 포함한 칸, 행·열 순."""
    return sorted(t.cells, key=lambda a: (a.r, a.c))


def _row_cells(t: Part, r: int) -> list[PCell]:
    return [a for a in _anchors(t) if a.r == r]


# ------------------------------------------------------------------ 괘선 기록 (table-meta recordRowRules)
def _rule_cover(hs, y, x1, x2) -> float:
    segs = sorted((max(s.x1, x1), min(s.x2, x2)) for s in hs if abs(s.y1 - y) <= RULE_Y_TOL and s.x2 > x1 and s.x1 < x2)
    ln, end = 0.0, x1
    for a, b in segs:
        if b > end:
            ln += b - max(a, end); end = b
    return ln / (x2 - x1) if x2 > x1 else 0.0


def row_rules(cells, bbox: Rect, hs) -> RowRules:
    """cells: [(Rect, filler)] — 클립 칸 상자. 괘선이 변 폭의 90% 이상을 덮으면 그어진 변."""
    real = [r for r, filler in cells if not filler]

    def edge(y, pick):
        w = cov = 0.0
        for c in real:
            if abs(pick(c) - y) <= 1:
                w += c.x2 - c.x1
                cov += _rule_cover(hs, y, c.x1, c.x2) * (c.x2 - c.x1)
        return w > 0 and cov >= 0.9 * w
    ir = io = 0
    for c in real:
        if abs(c.y1 - bbox.y1) <= 1:
            continue
        if _rule_cover(hs, c.y1, c.x1 + 0.5, c.x2 - 0.5) >= 0.9:
            ir += 1
        else:
            io += 1
    return RowRules(edge(bbox.y2, lambda c: c.y2), edge(bbox.y1, lambda c: c.y1), ir, io)


# ------------------------------------------------------------------ 열 경계 합집합 (unionCoords)
def _union_coords(a: list[float], b: list[float]):
    """앞 조각 경계는 그대로, 뒤 조각 경계는 가까운 쌍부터 앞 조각 경계와 짝짓는다. 짝 없는 뒤 경계는 새 경계."""
    drift = len(a) == len(b) and all(abs(x - y) <= PART_COL_TOL for x, y in zip(a, b))
    tol = PART_COL_TOL if drift else PART_COL_SNAP
    pairs = sorted((abs(x - v), i, v, x) for i, v in enumerate(b) for x in a if abs(x - v) <= tol)
    mp, taken = {}, set()
    for _, _, v, x in pairs:
        if v not in mp and x not in taken:
            mp[v] = x; taken.add(x)
    out = list(a)
    for v in b:
        if v not in mp:
            mp[v] = v; out.append(v)
    return sorted(out), mp


def _index_of(coords, x) -> int:
    best, bd = -1, float("inf")
    for i, v in enumerate(coords):
        if abs(v - x) < bd:
            best, bd = i, abs(v - x)
    return best if bd <= PART_COL_TOL else -1


def _row_text(anchors: list[PCell], r: int) -> str:
    return "|".join(_norm(a.text) for a in anchors if a.r == r)


def _has_text(t: str) -> bool:
    return t.replace("|", "") != ""


# ------------------------------------------------------------------ 쪼개진 행 합치기 (mergeSplitRow — 기하 증거만)
@dataclass
class _Cut:
    open: bool = False
    ruled: bool = False
    list: str = "any"                 # 목록 번호 증거 범위: any | multi | none (옮겨 맞댄 조각은 엄격하게)


# ------------------------------------------------------------------ 글 흐름 증거 (table-parts.ts · line-wrap.ts · split-row-pattern.ts)
FULL_LINE_TOL = 1.0         # 끝줄이 칸 글 오른끝에 이만큼(글자 높이 배) 안이면 꽉 찬 줄
FULL_LINE_MIN_FRAC = 0.5    # 끝줄 폭이 칸 폭의 이 비율 이상이어야 꽉 찬 줄
FULL_LINE_EDGE_MIN = 12.0   # 앞 쪽 한 줄짜리 칸: 끝줄과 칸 오른끝 사이 최대 거리(pt) 하한
SINGLE_LINE_WORD_GAP = 3.5  # 〃 어절 하나 폭(글자 높이 배)
SHORT_LINE_GAP = 1.5        # 왼쪽 정렬 증거 — 오른끝보다 글자 높이의 이 배수 넘게 짧은 줄
LABEL_ROOM = 1.5            # 끝난 이름표 — 한 줄 칸 오른쪽에 남은 자리가 글자 높이의 이 배수 이상
HANGING_MIN = 0.5           # 내어쓰기 — 글자 높이의 이 배수 이상 들어감
HANGING_TOL = 1.0           # 내어쓴 줄 왼끝이 같다고 보는 거리(pt)

ITEM_HEAD = re.compile(r"^(?:[□■◆◇○●◎◦▪▫•※▶▷►❍❏❑✓✔➢➤☞]|[①-⑳]|[-–·∙ㆍ*#](?=\s)|\(?\d{1,2}[.)](?!\d)|\(?[가-하][.)]"
                       r"|\([가-하\d]{1,2}\)|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|제\d+[조항호장절])")
ITEM_MARK = re.compile(r"^\s*(?:([가-힣])[.)]|(\d{1,3})[.)]|\(([가-힣\d]{1,3})\))(?=\s)")
KO_ITEMS = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허고노도로모보소오조초코토포호구누두루무부수우주추쿠투푸후"
KO_ORDER = "가나다라마바사아자차카타파하"
CLAUSE_OPEN_ENDING = re.compile(r"(?:하는|되는|하고|하며|하여|되어|되고|되며|이며|으며|어야|아야|여야)$")
OUTLINE_HEAD = re.compile(r"^[□■]\s*\S")
OUTLINE_ITEM = re.compile(r"^[ㅇ○◦\-‐–·․‧※*]\s*\S")


def _txt(a) -> str:
    """판정용 글 — 글줄을 줄바꿈으로 (kordoc 칸 글과 같은 모양)."""
    lt = getattr(a, "ltext", None)
    return "\n".join(lt) if lt else (a.text or "")


def starts_new_item(prev: str, nxt: str) -> bool:
    """다음 글이 새 항목 머리(번호·기호·조항)로 시작하나 (line-wrap.ts startsNewItem)."""
    t, p = nxt.lstrip(), prev.rstrip()
    return bool(ITEM_HEAD.match(t)) and not (t.startswith("다.") and re.search(r"[가-힣]$", p)) \
        and not (re.search(r"\(\d+\.\d*$", p) and re.match(r"^\d", t)) \
        and not (re.search(r"제\d+(?:조(?:의\d+)?|항)$", p) and re.match(r"^제\d+[항호]", t)) \
        and not (re.search(r"\d일$", p) and re.match(r"^\([월화수목금토일]\)", t))


def item_mark(line: str):
    """줄 머리 항목 번호 → (꼴, 차례) — 1. / (1) / 가. / (가) / ① / A. (split-row-pattern.ts itemMark)."""
    line = line.strip()
    if m := re.match(r"^(\d{1,2})([.)])(?!\d)", line):
        return "1" + m[2], int(m[1])
    if m := re.match(r"^\((\d{1,2})\)", line):
        return "(1)", int(m[1])
    if (m := re.match(r"^([가-하])([.)])", line)) and m[1] in KO_ORDER:
        return "가" + m[2], KO_ORDER.index(m[1])
    if (m := re.match(r"^\(([가-하])\)", line)) and m[1] in KO_ORDER:
        return "(가)", KO_ORDER.index(m[1])
    if m := re.match(r"^[①-⑳]", line):
        return "①", ord(m[0])
    if m := re.match(r"^([A-Z])\.\s", line):
        return "A.", ord(m[1])
    return None


def next_item_head(prev: str, nxt: str) -> bool:
    """뒤 글이 앞 글 첫머리 항목의 바로 다음 항목(같은 꼴)으로 시작 — "커." 다음 "터.", "3." 다음 "4."."""
    a, b = ITEM_MARK.match(prev), ITEM_MARK.match(nxt)
    if not a or not b:
        return False

    def succ(x, y):
        if x.isdigit():
            return y.isdigit() and int(y) == int(x) + 1
        return x in KO_ITEMS and y in KO_ITEMS and KO_ITEMS.index(y) == KO_ITEMS.index(x) + 1
    ps, ns = prev.lstrip(), nxt.lstrip()
    if a[1] and b[1]:
        return succ(a[1], b[1]) and ps[1:2] == ns[1:2]
    if a[2] and b[2]:
        return succ(a[2], b[2]) and ps[len(a[2]):len(a[2]) + 1] == ns[len(b[2]):len(b[2]) + 1]
    if a[3] and b[3]:
        return succ(a[3], b[3])
    return False


def _first_word_units(text: str) -> float:
    """첫 어절 폭(글자 높이 배) — 한글·한자 1, 나머지 0.55."""
    t = re.sub(r"^([(\[「『<〈])\s+", r"\1", text.strip())
    word = (t.split() or [""])[0]
    return sum(1.0 if re.match(r"[가-힣㐀-鿿]", ch) else 0.55 for ch in word)


def continues_across(u, d, x1, x2) -> bool:
    """앞 쪽 칸 글이 뒤 쪽 칸으로 이어지나 — 앞 쪽 끝줄이 칸 글 폭 오른끝까지 찼다(줄은 다음 어절이 안 들어갈 때만 바뀐다).
    가운데 정렬 칸은 끝줄이 가장 긴 줄일 뿐이어도 오른끝에 닿으므로, 왼쪽 정렬 증거(문단 끝 짧은 줄)가 있는 칸만 본다."""
    U, D = u.lines, d.lines
    if not U or not D:
        return False
    last = U[-1]
    fs = last[3] or 10.0
    minL = min(l[1] for l in U + D)
    maxR = max(l[2] for l in U + D)
    maxR = max(maxR, x2 - (minL - x1))                      # 칸 글 오른끝 — 왼쪽 안쪽 여백을 오른쪽에도 둔 자리까지
    if maxR - last[2] > FULL_LINE_TOL * fs:
        return False
    if len(U) == 1 and x2 - last[2] > max(FULL_LINE_EDGE_MIN, SINGLE_LINE_WORD_GAP * fs):
        return False
    indented = last[1] - minL >= HANGING_MIN * fs
    if last[2] - last[1] < FULL_LINE_MIN_FRAC * ((x2 - last[1]) if indented else (x2 - x1)):
        return False
    new_item = starts_new_item(_txt(u), _txt(d))
    allL = U + D

    def left_aligned(l):
        return maxR - l[2] > SHORT_LINE_GAP * fs and (
            l[1] - minL <= fs or (not new_item and any(o is not l and abs(o[1] - l[1]) <= HANGING_TOL and abs(o[2] - l[2]) >= HANGING_MIN * fs
                                                       for o in allL)))
    hanging = len(U) >= 2 and all(maxR - l[2] <= FULL_LINE_TOL * fs for l in U) and indented and abs(D[0][1] - last[1]) <= HANGING_TOL
    hanging_head = not indented and len(D) >= 2 and D[0][1] - last[1] >= HANGING_MIN * fs and maxR - D[0][2] <= FULL_LINE_TOL * fs \
        and all(abs(l[1] - D[0][1]) <= HANGING_TOL for l in D)
    d0 = D[0]
    hang_start = len(U) == 1 and not new_item and d0[1] - last[1] >= HANGING_MIN * fs and all(l[1] >= d0[1] - HANGING_TOL for l in D) \
        and ((abs(d0[2] - last[2]) <= HANGING_TOL and d0[2] - d0[1] >= FULL_LINE_MIN_FRAC * (x2 - x1))
             or (last[2] - d0[2] > SHORT_LINE_GAP * fs and abs(d0[1] + d0[2] - last[1] - last[2]) / 2 >= HANGING_MIN * fs))
    return any(left_aligned(l) for l in U) or any(left_aligned(l) for l in D) or hanging or hanging_head or hang_start


def line_ended(c, x1, x2, nxt=None) -> bool:
    """한 줄로 끝난 왼쪽 정렬 칸 — 오른쪽에 다음 어절이 들어갈 자리가 남았다 (끝난 이름표)."""
    if len(c.lines) != 1:
        return False
    l = c.lines[0]
    h = l[3] or 10.0
    d0 = nxt.lines[0] if nxt is not None and nxt.lines else None
    if d0 and d0[1] - l[1] >= HANGING_MIN * h:
        return False                                        # 뒤 첫 줄이 들어가 시작 — 앞 문단의 아래 항목
    return (x2 - l[2]) - (l[1] - x1) >= max(LABEL_ROOM, _first_word_units(_txt(nxt) if nxt is not None else "")) * h


def clause_continues(u, d) -> bool:
    """앞 쪽 칸 글이 문장 중간(목적격 조사·연결 어미)에서 끊기고 뒤 쪽이 새 항목 머리 아닌 한글로 시작."""
    toks = _txt(u).strip().split()
    tail, head = (toks[-1] if toks else ""), _txt(d).strip()
    if not re.fullmatch(r"[가-힣]{2,}", tail) or not re.match(r"^[가-힣]", head) or starts_new_item(_txt(u), head):
        return False

    def batchim(ch):
        k = ord(ch) - 0xAC00
        return 0 <= k < 11172 and k % 28 != 0
    last, before = tail[-1], tail[-2]
    return (last == "을" and batchim(before)) or (last == "를" and not batchim(before)) or bool(CLAUSE_OPEN_ENDING.search(tail))


def quantity_continues(u, d) -> bool:
    """앞이 수량으로 끝나고 뒤가 "미만·이상·이하·초과"로 연다."""
    return bool(re.search(r"\d[\d,.]*\s*(?:%|[가-힣]{1,2})?$", _txt(u).strip())) and bool(re.match(r"^(?:미만|이상|이하|초과)(?![가-힣])", _txt(d).strip()))


def word_overflows(u, d, x1, x2) -> bool:
    """앞 끝줄에 뒤 첫 어절을 붙이면 칸 폭을 넘는다 (칸 조각 이어짐과 함께만 쓴다)."""
    if not u.lines or starts_new_item(_txt(u), _txt(d)):
        return False
    last, units = u.lines[-1], _first_word_units(_txt(d))
    return units > 0 and last[2] - last[1] + units * (last[3] or 10.0) > x2 - x1


def list_continues(u, d, multi=False) -> bool:
    """뒤 첫 줄 번호가 앞 칸 목록의 다음 차례 (앞 목록은 번호 둘 이상이거나 첫 줄이 아닌 자리에서 시작)."""
    head = item_mark(_txt(d).strip())
    if not head:
        return False
    marks = [item_mark(l.strip()) for l in _txt(u).split("\n")]
    last, count = -1, 0
    for i, m in enumerate(marks):
        if m and m[0] == head[0]:
            last, count = i, count + 1
    return last >= 0 and marks[last][1] + 1 == head[1] and (count >= 2 or (not multi and last > 0))


def outline_continues(u, d) -> bool:
    U = [l.strip() for l in _txt(u).split("\n") if l.strip()]
    D = [l.strip() for l in _txt(d).split("\n") if l.strip()]
    return len(U) >= 2 and len(D) >= 1 and any(OUTLINE_HEAD.match(l) for l in U) and bool(OUTLINE_ITEM.match(D[0]))


class _Grid:
    """owner 격자와 칸 원점 조회 — kordoc table.cells[r][c] (덮인 자리는 빈 칸)."""
    EMPTY = PCell(-1, -1, 1, 1, "")

    def __init__(self, owner):
        self.owner, self.rows = owner, len(owner)

    def cell(self, r, c):
        if not 0 <= r < self.rows:
            return None
        o = self.owner[r][c]
        return o if o is not None and o.r == r and o.c == c else self.EMPTY


def has_numbered_row_sequence(G: _Grid, first, cols) -> bool:
    """연속된 네 완성행이 각각 항목 하나(차례 번호)와 독립된 옆칸을 가지면 쪽 경계도 새 항목 행이다."""
    if first < 3:
        return False
    rows = [first - 3, first - 2, first - 1, first]

    def separate(c, cs):
        for r in rows:
            a = G.owner[r][c]
            if a is None or a.r != r or a.c != c or a.rs != 1 or a.cs != cs or not _txt(a).strip():
                return False
        return True
    for c in range(cols):
        a = G.owner[first][c]
        if a is None or not separate(c, a.cs):
            continue
        marks = []
        for r in rows:
            ls = _txt(G.owner[r][c]).split("\n")
            fm = item_mark(ls[0])
            marks.append(fm if fm and not any(item_mark(l) for l in ls[1:]) else None)
        h = marks[0]
        if not h or any(not m or m[0] != h[0] or m[1] != h[1] + i for i, m in enumerate(marks)):
            continue
        for d in range(cols):
            side = G.owner[first][d]
            if side is not None and (d + side.cs <= c or d >= c + a.cs) and separate(d, side.cs):
                return True
    return False


def matches_independent_row_pair(G: _Grid, first, cols) -> bool:
    """같은 표 다른 자리에서 이미 독립된 두 행으로 되풀이된 내용 쌍이면 새 행 증거."""
    pairs, c = [], 0
    while c < cols:
        u, d = G.owner[first - 1][c], G.owner[first][c]
        if u is not None and u is d:
            c = u.c + u.cs
            continue
        ut = _norm(_txt(u)) if u is not None else ""
        dt = _norm(_txt(d)) if d is not None else ""
        if not ut and not dt:
            c += 1
            continue
        if u is None or d is None or u.r != first - 1 or d.r != first or u.c != c or d.c != c or u.rs != 1 or d.rs != 1 \
                or u.cs != d.cs or not ut or not dt:
            return False
        pairs.append((c, u.cs, ut, dt))
        c += u.cs
    if len(pairs) < 2 or all(p[2] == p[3] for p in pairs):
        return False
    for r in range(G.rows):
        if r + 1 >= G.rows or (r + 1 >= first - 1 and r <= first):
            continue
        ok = True
        for pc, pcs, ut, dt in pairs:
            for i, text in enumerate((ut, dt)):
                a = G.owner[r + i][pc]
                if a is None or a.r != r + i or a.c != pc or a.rs != 1 or a.cs != pcs or _norm(_txt(a)) != text:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            return True
    return False


def matches_complete_context_row(G: _Grid, first, cols) -> bool:
    """경계만 덮는 항목명과 합친 값이 같은 표의 완성행에 함께 있으면 이어진 칸."""
    context, value, c = [], None, 0
    while c < cols:
        u, d = G.owner[first - 1][c], G.owner[first][c]
        if u is not None and u is d:
            if u.r == first - 1 and u.rs == 2 and _txt(u).strip():
                context.append((c, u.cs, _norm(_txt(u))))
            c = u.c + u.cs
            continue
        ut = _txt(u).strip() if u is not None else ""
        dt = _txt(d).strip() if d is not None else ""
        if not ut and not dt:
            c += 1
            continue
        if value or u is None or d is None or u.r != first - 1 or d.r != first or u.c != c or d.c != c or u.rs != 1 or d.rs != 1 \
                or u.cs != d.cs or not ut or not dt:
            return False
        value = (c, u.cs, _norm(ut + dt))
        c += u.cs
    if not value or not context:
        return False
    proof = context + [value]
    for r in range(G.rows):
        if r in (first - 1, first):
            continue
        if all((a := G.owner[r][p[0]]) is not None and a.r == r and a.c == p[0] and a.rs == 1 and a.cs == p[1] and _norm(_txt(a)) == p[2]
               for p in proof):
            return True
    return False


def matches_complete_sibling_row(G: _Grid, first, cols) -> bool:
    """상위 항목("가. …")과 하위 목록("1) … 2) …")이 쪽 경계에서 갈렸는지, 같은 표 완성행으로 확인."""
    pairs, c = [], 0
    while c < cols:
        u, d = G.owner[first - 1][c], G.owner[first][c]
        if u is not None and u is d:
            c = u.c + u.cs
            continue
        if u is None or d is None or u.r != first - 1 or d.r != first or u.c != c or d.c != c or u.rs != 1 or d.rs != 1 or u.cs != d.cs:
            return False
        pairs.append((c, u.cs, u, d))
        c += u.cs
    filled = [p for p in pairs if _txt(p[2]).strip() and _txt(p[3]).strip()]
    if len(filled) != 1:
        return False
    main = filled[0]
    upper, lower = _txt(main[2]).strip(), _txt(main[3]).strip()
    if not re.match(r"^[가-힣]\.\s*\S", upper) or re.search(r"(?:^|\n)\s*\d+\)", upper):
        return False
    marks = [m for m in (re.match(r"^\s*(\d+)\)", s) for s in lower.split("\n")) if m]
    if not re.match(r"^1\)", lower) or len(marks) < 2 or any(int(m[1]) != i + 1 for i, m in enumerate(marks)):
        return False
    if any(p is not main and _txt(p[3]).strip() and not re.fullmatch(r"[○●◯✓✔]+", _txt(p[3]).strip()) for p in pairs):
        return False
    if not any(p is not main and _txt(p[2]).strip() and not _txt(p[3]).strip() for p in pairs):
        return False

    def nm(s, title):
        return re.sub(r"\s+", "", re.sub(r"^[가-힣]\.\s*", "", s) if title else s)
    for r in range(G.rows):
        if r in (first - 1, first):
            continue
        if all((a := G.owner[r][p[0]]) is not None and a.r == r and a.c == p[0] and a.rs == 1 and a.cs == p[1]
               and nm(_txt(a).strip(), p is main) == nm(_txt(p[2]).strip() + "\n" + _txt(p[3]).strip(), p is main) for p in pairs):
            return True
    return False


def matches_complete_rubric_row(G: _Grid, first, cols) -> bool:
    """분류칸이 이어지고, 완성된 평가행의 번호(A. B. …)·점수열·마지막 항목까지 같을 때만 목록 조각을 잇는다."""
    pairs, carried, c = [], False, 0
    while c < cols:
        u, d = G.owner[first - 1][c], G.owner[first][c]
        if u is not None and u is d:
            if u.r == first - 1 and u.rs == 2 and _txt(u).strip():
                carried = True
            c = u.c + u.cs
            continue
        if u is None or d is None or u.r != first - 1 or d.r != first or u.c != c or d.c != c or u.rs != 1 or d.rs != 1 or u.cs != d.cs \
                or not _txt(u).strip() or not _txt(d).strip():
            return False
        pairs.append((c, u.cs, u, d))
        c += u.cs
    if not carried or len(pairs) != 2:
        return False

    def values(a):
        return [s.strip() for s in _txt(a).strip().split("\n") if s.strip()]
    score = next((p for p in pairs if all(re.fullmatch(r"-?\d+(?:\.\d+)?", s) for s in values(p[2]) + values(p[3]))), None)
    if not score:
        return False
    main = next(p for p in pairs if p is not score)

    def items(s):
        return [(ord(m[1]), m.start(1)) for m in re.finditer(r"(?:^|\n)\s*([A-Z])\.\s+", s)]
    upper, lower = _txt(main[2]).strip(), _txt(main[3]).strip()
    um, dm = items(upper), items(lower)
    marks = um + dm
    if not re.match(r"^A\.\s", upper) or not re.match(r"^B\.\s", lower) or len(um) != 1 or len(dm) < 2 \
            or any(m[0] != 65 + i for i, m in enumerate(marks)):
        return False
    ups, downs = values(score[2]), values(score[3])
    if len(ups) != len(um) or len(downs) != len(dm):
        return False
    tail = _norm(lower[dm[-1][1]:])
    for r in range(G.rows):
        if r in (first - 1, first):
            continue
        if not all((a := G.owner[r][p[0]]) is not None and a.r == r and a.c == p[0] and a.rs == 1 and a.cs == p[1] for p in pairs):
            continue
        peer = _txt(G.owner[r][main[0]]).strip()
        pm = items(peer)
        if len(pm) == len(marks) and all(x[0] == y[0] for x, y in zip(pm, marks)) \
                and "|".join(values(G.owner[r][score[0]])) == "|".join(ups + downs) and _norm(peer[pm[-1][1]:]) == tail:
            return True
    return False


def _merge_split_row(cells: list[PCell], owner, first: int, cols: int, colXs, cut: _Cut, text_only=False) -> bool:
    """쪽 경계 행(first-1 | first)이 한 행이 쪽에서 갈린 것이면 합친다 (kordoc mergeSplitRow).
    증거: 1) 글 이어짐(끝줄이 꽉 참·문장 중간·수량·목록 번호) 2) 칸 조각 이어짐(클립 없는 빈 자리) 3) 괘선 없는 쪽 경계."""
    last = first - 1
    rows = len(owner)
    if first >= rows:
        return False
    G = _Grid(owner)

    def content(a):
        return bool(a.text.strip())
    starts, c = [], 0
    while c < cols:
        b = owner[first][c]
        if b is not None and b.r == first:
            starts.append(b)
        c = b.c + b.cs if b is not None else c + 1
    tall = [b for b in starts if b.rs > 1 and content(b)]
    band = not text_only and bool(tall) and all(b.rs > 1 or not content(b) for b in starts) and all(
        (a := owner[last][b.c]) is not None and a.rs > 1 and a.c == b.c and a.cs == b.cs and a.r + a.rs - 1 == last and content(a)
        for b in tall)
    pairs, carried, c = [], False, 0
    while c < cols:
        a, b = owner[last][c], owner[first][c]
        if a is None and b is None:
            if colXs[c + 1] - colXs[c] >= CARRIED_MIN_COL_W:
                carried = True                       # 두 쪽 모두 클립 없던 자리 — 글 없는 칸이 쪽을 넘은 부분
            c += 1
            continue
        if a is None or b is None:
            return False
        if a is not b:
            if a.c != b.c or a.cs != b.cs or a.r + a.rs - 1 != last or b.r != first:
                return False
            if b.rs > 1 and (a.rs > 1 or not content(a)) and not band:
                return False
            pairs.append((a, b))
        elif a.r == last and a.rs == 2:
            carried = True                           # 앞 쪽 끝 행 칸을 뒤 쪽 첫 행 빈 자리로만 이어 늘렸다
        c = a.c + a.cs
    new_cell = any(not content(u) and content(d) for u, d in pairs)

    def serial(a):
        t = a.text.strip()
        return int(t) if SERIAL_RE.match(t) else None
    if any((s := serial(u)) is not None and serial(d) == s + 1 for u, d in pairs):
        return False                                 # 번호 열이 다음 차례 — 새 행
    if cut.ruled and (has_numbered_row_sequence(G, first, cols) or matches_independent_row_pair(G, first, cols)):
        return False
    content_pairs = [(u, d) for u, d in pairs if content(u) and content(d)]

    def x12(a):
        return colXs[a.c], colXs[a.c + a.cs]
    # 칸 조각 이어짐: 글 있는 열 쌍이 하나뿐이거나, 뒤 조각 글 칸이 모두 여러 행(함께 넘어간 세로 병합)이거나, 괘선 없는 경계에서 줄 넘침
    carried_split = not text_only and carried and not new_cell and (
        len(content_pairs) <= 1 or all(d.rs > 1 for _, d in content_pairs)
        or (not cut.ruled and any(word_overflows(u, d, *x12(u)) for u, d in content_pairs)))
    if not carried_split and not band:
        open_cut = cut.open and not new_cell
        # 앞 칸이 한 줄로 끝난 이름표인데 뒤 같은 열에 다른 글 — 새 칸 (같은 글이면 문단마다 붙는 표지 "<신 설>")
        if not open_cut and any(content(d) and _norm(d.text) != _norm(u.text) and line_ended(u, *x12(u), d) for u, d in pairs):
            return False
        # 한 칸은 다음 항목 머리로 시작하고("커." / "터."), 다른 칸은 두 조각 모두 서로 다른 한 줄 값 — 새 행
        if not open_cut and any(next_item_head(_txt(u), _txt(d)) for u, d in pairs) and any(
                content(u) and content(d) and len(u.lines) == 1 and len(d.lines) == 1 and _norm(u.text) != _norm(d.text) for u, d in pairs):
            return False
        # 여러 행 칸에 한 줄만 든 앞 칸(칸이 차지 않아 넘칠 수 없음) 옆 다른 한 줄 값 — 새 행
        if cut.ruled and any(u.rs >= 2 and content(u) and content(d) and len(u.lines) == 1 and len(d.lines) == 1
                             and _norm(u.text) != _norm(d.text) for u, d in pairs):
            return False

        def flows(u, d):
            return continues_across(u, d, *x12(u)) or clause_continues(u, d) or quantity_continues(u, d)
        if not open_cut:                             # 괘선 없는 쪽 경계(3)는 글 증거 없이 합친다
            if any(d.rs > 1 and not flows(u, d) for u, d in pairs):
                return False                         # 다음 쪽에서 새로 시작한 세로 병합 칸
            one = [(u, d) for u, d in pairs if d.rs == 1]
            filled = [(u, d) for u, d in pairs if content(d)]
            outline_only = len(filled) == 1 and outline_continues(*filled[0])
            if not matches_complete_sibling_row(G, first, cols) and not matches_complete_rubric_row(G, first, cols) \
                    and not (cut.ruled and matches_complete_context_row(G, first, cols)) and not outline_only \
                    and not any(flows(u, d) for u, d in one) \
                    and (cut.list == "none" or not any(list_continues(u, d, cut.list == "multi") for u, d in one)):
                return False
    gone = set()
    for u, d in pairs:
        if d.text.strip():
            u.text = u.text + "\n" + d.text if u.text.strip() else d.text
        u.ltext = list(u.ltext) + list(d.ltext)
        u.lines = list(u.lines) + list(d.lines)      # 끝줄이 뒤 조각 끝줄이 되게 (세 쪽 넘게 이어질 때 글 이어짐 판정)
        if u.edges and d.edges:
            u.edges = Edges(u.edges.t, d.edges.b, u.edges.l or d.edges.l, u.edges.r or d.edges.r)
        if d.rs > 1:
            u.rs = u.rs + d.rs
        gone.add(id(d))
    keep = []
    for a in cells:
        if id(a) in gone:
            continue
        if a.r < first and a.r + a.rs > first:
            a.rs -= 1                                # 위에서 내려와 두 행에 걸친 칸은 한 행 줄어든다
        elif a.r == first:
            if a.rs <= 1:
                continue                             # 경계 행에서만 시작한 빈 자리 칸
            a.rs -= 1
        elif a.r > first:
            a.r -= 1
        keep.append(a)
    cells[:] = keep
    return True


# ------------------------------------------------------------------ 두 조각 잇기 (joinSplitParts)
@dataclass
class _Through:
    """앞 조각 왼쪽에 빠진 열을 쪼개진 행 판정에서 한 칸으로 보게 하는 자리표 (출력에는 안 나간다)."""
    r: int
    c: int
    rs: int
    cs: int
    text: str = ""
    lines: list = field(default_factory=list)
    ltext: list = field(default_factory=list)


def join_split_parts(prev: Part, pcx, curr: Part, ccx, dx=0.0):
    """두 조각을 합집합 격자로 잇는다. 경계 대응이 안 되면 None. → (cells, rows, cols, U, split, header, rules)."""
    if len(pcx) != prev.cols + 1 or len(ccx) != curr.cols + 1:
        return None
    U, mp = _union_coords(pcx, ccx)
    cxU = [mp.get(v, v) for v in ccx]
    cols = len(U) - 1
    if cols < 1:
        return None
    pa, ca = _anchors(prev), _anchors(curr)
    pr, cr = prev.rules, curr.rules
    # 반복 머리 행 — 쪽 경계가 괘선 없이 열렸으면 뒤 조각 첫 행은 갈린 칸의 이어짐이지 머리 행이 아니다
    cut_open = pr is not None and cr is not None and not pr.bottom and not cr.top
    skip = head = 0
    while not cut_open and skip < min(3, curr.rows - 1, prev.rows) and _row_text(ca, skip) != "" \
            and _row_text(ca, skip) == _row_text(pa, skip):
        skip += 1
    h = 1
    while h <= 2 and not skip and not cut_open:     # 표 첫 조각은 머리 행 위에 제목 상자가 붙어 나올 수 있다
        head = h
        while skip < min(3, curr.rows - 1, prev.rows - h) and _has_text(_row_text(ca, skip)) \
                and _row_text(ca, skip) == _row_text(pa, h + skip):
            skip += 1
        h += 1

    def same_spans(u, d):
        # 되풀이 머리 행은 원래 머리 행과 칸 자리(x 범위)까지 같다 (짝·홀 쪽 여백만큼 통째로 옮겨질 뿐)
        if len(u) != len(d) or not u:
            return False
        shift = pcx[u[-1].c + u[-1].cs] - ccx[d[-1].c + d[-1].cs]
        return all(abs(pcx[a.c] - ccx[b.c] - shift) <= CONTINUATION_COL_TOL
                   and abs(pcx[a.c + a.cs] - ccx[b.c + b.cs] - shift) <= CONTINUATION_COL_TOL for a, b in zip(u, d))
    for k in range(skip):
        if same_spans([a for a in pa if a.r == head + k], [a for a in ca if a.r == k]):
            continue
        if _has_text(_row_text(ca, k)):
            return None                              # 글만 같고 칸 자리가 다르면 같은 머리를 단 새 표
        skip = k
        break

    rows = prev.rows + curr.rows - skip
    owner = [[None] * cols for _ in range(rows)]
    out: list[PCell] = []
    edged = False

    def place(a: PCell, x, row_off, keep_box, shift=0.0) -> bool:
        nonlocal edged
        c1, c2 = _index_of(U, x[a.c]), _index_of(U, x[a.c + a.cs])
        if c1 < 0 or c2 <= c1:
            return False
        r = a.r + row_off
        # 글줄도 옮긴 거리만큼 옮겨 앞 조각 글줄과 맞댄다 (짝·홀 쪽 대칭 여백)
        lines = [(y, l + shift, rr + shift, h) for y, l, rr, h in a.lines] if shift else list(a.lines)
        p = PCell(r, c1, a.rs, c2 - c1, a.text, a.edges, a.box if keep_box else None, lines=lines, ltext=list(a.ltext))
        for dr in range(a.rs):
            for dc in range(c1, c2):
                if r + dr < rows:
                    owner[r + dr][dc] = p
        out.append(p)
        if a.edges is not None:
            edged = True
        return True
    # 메운 칸(클립 없던 자리)은 놓지 않는다 — 빈 자리로 남아 아래 세로 병합 잇기가 채운다
    for a in pa:
        if not a.filler and not place(a, pcx, 0, True):
            return None
    for a in ca:
        if a.r < skip or a.filler:
            continue
        if not place(a, cxU, prev.rows - skip, False, dx):
            return None

    # 뒤 조각 첫 행부터 비어 있는 열 — 앞 조각 마지막 행에서 그 열을 덮던 칸의 세로 병합을 잇는다
    first = prev.rows
    for c in range(cols):
        above = owner[first - 1][c] if first >= 1 else None
        if above is None:
            continue
        r = first
        while r < rows and owner[r][c] is None:
            r += 1
        if r == first:
            continue
        span = r - first
        if any(owner[rr][dc] is not None for dc in range(above.c, above.c + above.cs) for rr in range(first, first + span)):
            continue                                 # 자기 폭 전부가 같은 구간만큼 비어 있을 때만
        above.rs += span
        for rr in range(above.r, above.r + above.rs):
            for dc in range(above.c, above.c + above.cs):
                owner[rr][dc] = above
    # 아무 칸도 덮지 않은 자리는 메운 칸으로 남긴다 (세 쪽 넘게 이어질 때 다시 빈 자리로 본다)
    for r in range(rows):
        for c in range(cols):
            if owner[r][c] is None:
                out.append(PCell(r, c, 1, 1, "", NO_EDGES if edged else None, None, filler=True))
    # 앞 조각 왼쪽에 빠진 열 — 쪽 전체를 덮은 세로 병합 칸이 클립 없이 지나간 자리. 쪼개진 행 판정에는 한 칸으로 보인다
    view = [row[:] for row in owner]
    c = 0
    while c < cols and U[c + 1] <= pcx[0] + PART_COL_TOL:
        r = prev.rows
        while r < rows and owner[r][c] is None:
            r += 1
        th = _Through(0, c, r, 1)
        for rr in range(r):
            view[rr][c] = th
        c += 1
    # 옮겨 맞댄 조각(짝·홀 쪽)은 쪽마다 새로 놓인 상자일 수 있다 — 한 칸 제목 행을 되풀이했으면 목록 번호 이어짐을 증거로 쓰지 않고,
    # 아니어도 앞 조각 목록 번호가 둘 이상일 때만 쓴다
    head_cells = sum(1 for a in ca if a.r < skip and a.text.strip())
    lst = "any" if dx == 0 else ("none" if skip > 0 and head_cells < 2 else "multi")
    cut = _Cut(open=bool(pr and cr and not pr.bottom and not cr.top and pr.inner_open + cr.inner_open == 0
                         and pr.inner_ruled + cr.inner_ruled > 0),
               ruled=bool(pr and cr and pr.bottom and cr.top), list=lst)
    split = False
    if rows > prev.rows:
        # 메운 칸은 owner 에 없다(None) — 원본처럼 '두 쪽 모두 클립 없는 자리'로 본다
        split = _merge_split_row(out, view, first, cols, U, cut)
        if split:
            rows -= 1
    rules = None
    if pr and cr:
        cut_ruled = pr.bottom or cr.top
        rules = RowRules(pr.top, cr.bottom, pr.inner_ruled + cr.inner_ruled + (1 if not split and cut_ruled else 0),
                         pr.inner_open + cr.inner_open + (1 if not split and not cut_ruled else 0))
    # 머리 행 증거는 되풀이된 행에 글 있는 칸이 둘 이상일 때만
    header = sum(1 for a in ca if a.r < skip and a.text.strip()) >= 2
    return out, rows, cols, U, split, header, rules


# ------------------------------------------------------------------ 쪽 넘김 판정
def _shifted_same(px, cx, allow_shift=True) -> bool:
    if len(px) != len(cx):
        return False
    dx = px[-1] - cx[-1] if allow_shift else 0.0
    return all(abs(x - y - dx) <= CONTINUATION_COL_TOL for x, y in zip(px, cx))


def _first_text(t: Part) -> str:
    for a in _row_cells(t, 0):
        if a.text.strip():
            return a.text
    return ""


def looks_continued(prev: Part, curr: Part) -> bool:
    """앞 표가 쪽 아래 16% 안까지 차고 뒤 표가 쪽 위 16% 안에서 시작하며, 뒤 표 첫 칸이 첨부 머리표가 아니다."""
    pb = prev.tail[1] if prev.tail else prev.bbox
    if pb.y1 > prev.page_h * PAGE_EDGE_BAND or curr.bbox.y2 < curr.page_h * (1 - PAGE_EDGE_BAND):
        return False
    if not ANNEX_HEAD_RE.search(_first_text(curr)):
        return True
    filled = [_norm(a.text) for a in _row_cells(curr, 0) if _norm(a.text)]
    return len(filled) >= 2 and all(t == filled[0] for t in filled)   # 신구조문 대비표 본문 행


def _prev_lacks_left_cols(prev: Part, px, xs) -> bool:
    if prev.bbox.y2 < prev.page_h * (1 - PAGE_EDGE_BAND):
        return False
    return xs[0] < px[0] and any(abs(x - px[0]) <= CONTINUATION_COL_TOL for x in xs[1:-1])


def _heading_row(row: list[PCell]) -> bool:
    heads = [_norm(a.text) for a in row if _norm(a.text)]
    if len(heads) == 1:
        return len(heads[0]) <= 8 or bool(re.match(r"^[①-⑳❶-❿]", heads[0]))
    return len(heads) == 2 and bool(re.match(r"^(?:\d{1,2}|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+|[①-⑳❶-❿])$", heads[0]))


def join_clip_parts(prev: Part, curr: Part) -> Part | None:
    """클립 표 두 조각이 한 표의 쪽 넘김이면 이어 붙인 Part, 아니면 None."""
    px, cx = prev.colXs, curr.colXs
    xs, shifted, dx, foreign, within = cx, False, 0.0, False, False
    if not _shifted_same(px, cx, False):
        dx = px[-1] - cx[-1]
        # 뒤 조각 경계가 모두 앞 조각 경계 위 — 옮겨진 게 아니라 양옆 열이 빠진 조각 (쪼개진 행 증거 필요)
        within = abs(dx) > CONTINUATION_COL_TOL and len(cx) >= 3 and all(any(abs(p - x) <= CONTINUATION_COL_TOL for p in px) for x in cx)
        if abs(dx) > CONTINUATION_COL_TOL and not within:
            xs = [x + dx for x in cx]; shifted = True       # 짝·홀 쪽 대칭 여백만큼 통째로 옮겨짐
        else:
            dx = 0.0
        if not any(abs(x - xs[0]) <= CONTINUATION_COL_TOL for x in px) and not _prev_lacks_left_cols(prev, px, xs):
            return None

        def unrelated(b):
            inner = [x + dx for x in b[1:-1]]
            return bool(inner) and not any(abs(p - x) <= CONTINUATION_COL_TOL for x in inner for p in px)
        # 안쪽 열 경계가 하나도 안 맞물리고 제목 상자 꼴/여러 칸 첫 행으로 시작하면 새로 놓인 표
        foreign = unrelated(cx) and unrelated(curr.part_colXs or cx) and (
            _heading_row(_row_cells(curr, 0)) or (len(px) > FOREIGN_FIRST_ROW_CELLS and len(_row_cells(curr, 0)) >= FOREIGN_FIRST_ROW_CELLS))
    res = join_split_parts(prev, px, curr, xs, dx)
    if res is None:
        return None
    cells, rows, cols, U, split, header, rules = res
    if (shifted or foreign or within) and not split and not header:
        return None
    return Part(prev.page, prev.page_h, prev.bbox, U, rows, cols, cells, rules, None, prev.part_colXs or px, curr.tail or (curr.page, curr.bbox),
                prev.joined_pages + [curr.page] + curr.joined_pages)


def _first_row_sig(t: Part):
    if len(t.colXs) != t.cols + 1:
        return None
    x = t.colXs
    return [(x[a.c], x[a.c + a.cs], _norm(a.text)) for a in _row_cells(t, 0) if not a.filler]


def _restarts_titled_form(head: Part, curr: Part) -> bool:
    if head.rows < 2 or curr.rows < 2 or head.cols != curr.cols:
        return False

    def full(t):
        r0 = [a for a in _row_cells(t, 0) if not a.filler]
        return len(r0) == 1 and r0[0].c == 0 and r0[0].cs == t.cols
    if not full(head) or not full(curr):
        return False
    ht, ct = _norm(_row_cells(head, 0)[0].text), _norm(_row_cells(curr, 0)[0].text)
    if not ht or not ct or ht == ct:
        return False

    def title_like(t):
        return len(t) <= TITLE_ROW_MAX_CHARS and not re.match(r"^[ㅇ○◦•·\-–※*□■▪☞]", t)
    if not title_like(ht) or not title_like(ct):
        return False

    def spans(t):
        return ",".join(f"{a.c}:{a.cs}" for a in _row_cells(t, 1))
    lab = next((_norm(a.text) for a in _row_cells(head, 1) if a.c == 0), "")
    lab2 = next((_norm(a.text) for a in _row_cells(curr, 1) if a.c == 0), "")
    return spans(head) == spans(curr) and lab != "" and lab == lab2


def _restarts_table(head: Part, curr: Part) -> bool:
    """뒤 표 첫 행이 사슬 첫 조각의 첫 행을 다시 시작하는가 (칸 짜임이 같고 이름표 둘 이상 같은데 값 칸이 다르다)."""
    cs = _first_row_sig(curr)
    if not cs:
        return False
    if len(cs) == 1 and _restarts_titled_form(head, curr):
        return True
    if len(cs) < RESTART_MIN_ANCHORS:
        return False
    if CONTACT_HEAD.search(cs[0][2]) and any(CONTACT_ROLE.search(c[2]) for c in cs):
        return False
    hs = _first_row_sig(head)
    if not hs or len(hs) != len(cs):
        return False
    dx = hs[-1][1] - cs[-1][1]
    if not all(abs(h[0] - c[0] - dx) <= CONTINUATION_COL_TOL and abs(h[1] - c[1] - dx) <= CONTINUATION_COL_TOL for h, c in zip(hs, cs)):
        return False
    same = diff = 0
    for h, c in zip(hs, cs):
        if not h[2] and not c[2]:
            continue
        if h[2] == c[2]:
            same += 1
        else:
            diff += 1
    return same >= 2 and diff >= 1


def _starts_with_unit_row(t: Part) -> bool:
    r0 = sorted(_row_cells(t, 0), key=lambda a: a.c)
    return bool(r0) and bool(UNIT_ROW_RE.match(r0[0].text)) and all(not a.text.strip() for a in r0[1:])


# ------------------------------------------------------------------ 한 칸 이어짐 (mergeContinuedCells)
def _last_row_cell(t: Part, x1, x2):
    xs = t.colXs
    for a in _anchors(t):
        if a.filler or a.r + a.rs != t.rows:
            continue
        if abs(xs[a.c] - x1) <= COL_MATCH_TOL and abs(xs[a.c + a.cs] - x2) <= COL_MATCH_TOL:
            return a
    return None


# ------------------------------------------------------------------ 문서 단위
def merge_parts(parts: list[Part], gap_free) -> list[Part]:
    """쪽 순(쪽 안은 위→아래) 최상위 클립 표 조각 → 이은 표 목록.

    gap_free(page_a, y_a, page_b, y_b): 앞 쪽 y_a(앞 조각 밑변) 아래와 뒤 쪽 y_b(뒤 조각 윗변) 위에 쪽 번호·머리말 띠 말고
      글이 없나 — kordoc 은 사이 블록이 없어야 잇는다. 우리는 단어 위치로 본다.
    """
    blocks = list(parts)
    # 1) 한 칸 이어짐 조각을 앞 쪽 표 칸에 붙인다 (뒤에서부터)
    j = len(blocks) - 1
    while j > 0:
        part = blocks[j]
        if part.cont is not None:
            i = j - 1
            prev = blocks[i]
            if prev.page == part.page - 1:
                cell = _last_row_cell(prev, *part.cont)
                add = next((a for a in part.cells if not a.filler), None)
                if cell is not None and add is not None:
                    if add.text.strip():
                        cell.text = cell.text + "\n" + add.text if cell.text.strip() else add.text
                    cell.ltext = list(cell.ltext) + list(add.ltext)
                    cell.lines = list(cell.lines) + list(add.lines)
                    if cell.edges and add.edges:
                        cell.edges = Edges(cell.edges.t, add.edges.b, cell.edges.l or add.edges.l, cell.edges.r or add.edges.r)
                    prev.tail = part.tail or (part.page, part.bbox)
                    prev.joined_pages = prev.joined_pages + [part.page] + part.joined_pages
                    blocks.pop(j)
        j -= 1

    def tail_page(b: Part):
        return b.tail[0] if b.tail else b.page

    def tail_box(b: Part):
        return b.tail[1] if b.tail else b.bbox

    def inside(b: Part, t: Part):
        if b.page != t.page:
            return False
        o = t.bbox
        return b.bbox.x1 >= o.x1 - 1 and b.bbox.x2 <= o.x2 + 1 and b.bbox.y1 >= o.y1 - 1 and b.bbox.y2 <= o.y2 + 1

    def left_of(b: Part, t: Part):
        return b.page == t.page and b.bbox.x2 <= t.bbox.x1 + 1

    def continued(p: Part, c: Part) -> bool:
        if tail_page(p) + 1 != c.page or not looks_continued(p, c):
            return False
        return gap_free(tail_page(p), tail_box(p).y1, c.page, c.bbox.y2)

    def chain_head(i: int) -> int:
        k = i
        for _ in range(RESTART_LOOKBACK_PAGES):
            cur = blocks[k]
            p = k - 1
            if p < 0:
                break
            for q in range(p - 1, -1, -1):           # 칸 안 예시 상자처럼 앞 표 안에 든 표면 그 앞 표로
                if blocks[q].page != blocks[p].page:
                    break
                if inside(blocks[p], blocks[q]):
                    p = q
                    break
            pb = blocks[p]
            if tail_page(pb) != cur.page - 1 or not continued(pb, cur):
                break
            px, cx = pb.colXs, cur.colXs
            dx = px[-1] - cx[-1]
            if not _shifted_same(px, cx) and not any(abs(x - (cx[0] + dx)) <= CONTINUATION_COL_TOL for x in px) \
                    and not _prev_lacks_left_cols(pb, px, cx):
                break
            k = p
        return k

    # 2) 쪽 넘김 조각 잇기 (뒤에서부터 — 세 쪽 넘게 이어지면 뒤 조각부터 모인다)
    i = len(blocks) - 2
    while i >= 0:
        if i + 1 >= len(blocks):
            i -= 1
            continue
        tail = blocks[i]
        j = i + 1
        while j < len(blocks) and (inside(blocks[j], tail) or left_of(blocks[j], tail)) and blocks[j].page <= tail_page(tail) + 1:
            j += 1
        if j >= len(blocks):
            i -= 1
            continue
        prev, curr = blocks[i], blocks[j]
        if curr.page != tail_page(prev) + 1:
            i -= 1
            continue
        if _starts_with_unit_row(prev) and _starts_with_unit_row(curr):
            i -= 1
            continue
        joined = None
        is_cont = continued(prev, curr) and not _restarts_table(blocks[chain_head(i)], curr)
        if is_cont:
            joined = join_clip_parts(prev, curr)
        if joined is not None:
            blocks[i] = joined
            blocks.pop(j)
            i -= 1
            continue
        if curr.empty:
            blocks.pop(j)                            # 앞 표의 이어짐이 못 된 빈 조각은 버리고 다시 본다
            continue
        i -= 1
    # 잇지 못한 빈 조각은 버린다
    return [b for b in blocks if not b.empty]
