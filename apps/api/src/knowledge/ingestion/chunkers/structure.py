# -*- coding: utf-8 -*-
"""구조 기반 청커 (2차 청킹 실험의 C2, chunkexp/ours_v2.py에서 옮김. 알고리즘 그대로).
실험 결과: 청킹실험_결과서 — 글 빠짐 0, 표 온전, 내규 문서 계층 복원 100%, 검색 Recall@5 100%(test3).

C2 = C 보완판 (2026-11-20, test2 원인 확인 뒤, test3 전). C1(ours.py)과 다른 점 3가지만:
  ① 목차 처리: "목차/목 차/차례/CONTENTS" 제목과 목차 줄(document_index 또는 점선+쪽번호 줄)을 한 단위로 묶고,
     본문이 시작되면 목차 제목을 경로에서 닫는다. 목차 줄은 제목 후보가 아니다.
  ② 번호 종류 추가: 하이픈 번호("1-1.", "1-1)"), 질문 번호("Q1", "Q 1.", "Q1.").
  ③ 강제 자르기: 띄어쓰기로도 상한 안에 못 드는 덩어리는 글자 단위로 자른다(글 보존 유지).

(아래는 C1 설명 그대로)
C 우리 청커: 번호 사다리로 구조를 다시 세우고, 구조 경계에서만 나누고 합친다.

바탕: 종원이형 인수인계(설계 결정 13개, 6장 번호 체계, 8장 함정) + 계획서 4-1 함정 15개.
입력은 파서 결과 JSON(Doc)만. 파서·Docling 청커 코드를 부르지 않는다.

단계
  1) 줄 나누기: 항목마다 제목 머리를 떼어 낸다. "제1조(목적) 이 법은…"이 한 항목이면 "제1조(목적)"=제목, 나머지=본문 (함정 1·5)
  2) 제목 판정과 단계
       법령형(조 3개 이상): 편 > 장 > 절 > 관 > 조, 부칙·별표·별지·서식은 맨 위로 되돌아감. 항(①)·호(1.)·목(가.)은 제목이 아님
       공문형: Ⅰ. → 1. → 가. → 1) → 가) → (1) → (가) → ① → ㉮, 소수점(2.1), "첫째," 순서.
               같은 제목 아래에서 처음 나온 기호 종류 순서대로 쌓는다(같은 종류 = 형제, 처음 보는 종류 = 자식).
               번호 줄이 문장으로 끝나면(…다.) 제목이 아니라 본문 항목이다.
       번호 없는 파서 제목(상자 제목 등)은 지금 번호 제목의 자식으로 둔다 (함정 6)
  3) 단위 = 제목 하나 ~ 다음 제목 직전. 내용 없는 제목은 청크를 만들지 않고 자식 청크 경로에 남긴다 (함정 13)
     단, 자식이 없는 내용 없는 제목(목록 같은 제목 줄)은 글이 사라지지 않게 본문으로 넣는다
  4) 길면 나누기: 항 → 호 → 목 → 문단 → 문장 → (그래도 크면) 띄어쓰기. 문장 중간에서 안 자름
     표: 캡션·바로 앞 짧은 안내 문장과 묶음. 크면 행 묶음으로 나누고 묶음마다 머리행 반복 (함정 7)
     목록: 중간에서 끊지 않음(넘칠 때만 항목 경계에서)
  5) 짧으면 합치기: 같은 부모 아래 형제끼리, 짧은 청크(min_tokens 미만)만, 합쳐도 상한 이하일 때만.
     합칠 때 빠지는 제목은 본문 첫 줄로 남김 (함정 14)
  6) 글 두 가지: content = 원문(표는 마크다운), embed_text = "문서 제목 > 장 > 조" + 줄바꿈 + 본문(표는 "열이름: 값") (설계 5·6)
     둘 다 상한 안에 들게 나눔 (함정 10)
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field

from .doc import Doc, Seg, norm

_COUNT: ContextVar = ContextVar("chunker_count_tokens")


def count_tokens(text: str) -> int:
    """지금 청킹 호출에 넣은 토큰 세기 함수(임베딩 모델 토크나이저 기준)."""
    return _COUNT.get()(text)

MIN_TOKENS = 64          # 이보다 짧은 청크만 형제와 합친다 (11/20 C 첫 실행 전 결정)

# ------------------------------------------------------------------ 번호 기호
LAW_HEAD = [  # (종류, 단계, 정규식) — 줄 머리에서만
    ("편", 1, re.compile(r"^제\s*\d+\s*편(?:의\s*\d+)?(?=\s|$)")),
    ("장", 2, re.compile(r"^제\s*\d+\s*장(?:의\s*\d+)?(?=\s|$)")),
    ("절", 3, re.compile(r"^제\s*\d+\s*절(?:의\s*\d+)?(?=\s|$)")),
    ("관", 4, re.compile(r"^제\s*\d+\s*관(?:의\s*\d+)?(?=\s|$)")),
]
JO = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*\([^()\n]{1,60}\)")        # 조 제목은 괄호 제목이 있어야 (함정 2)
JO_BARE = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?(?=\s*$)")                 # "제5조" 한 줄 (괄호 없음, 단독 줄일 때만)
JO_PLAIN = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?\s+[가-힣ㆍ·\s]{2,30}$")      # "제1조 적용범위" (약관식, 단독 짧은 줄, 11/20 dev 수정)
SPECIAL = re.compile(r"^(?:부\s*칙(?=\s|<|\(|$)|\[\s*별\s*표\s*\d*[^\]]*\]|<\s*별\s*표\s*\d*[^>]*>|\[\s*별\s*지[^\]]*\]|별\s*표\s*\d+(?=\s|$)|\[\s*서\s*식[^\]]*\])")
FAMILIES = [  # 공문형 번호 종류 (기호 뒤 글자 확인: 함정 3)
    ("로마", re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\s*[.．]?(?=\s|[가-힣A-Za-z])")),
    ("소수3", re.compile(r"^\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\.?(?=\s)")),
    ("소수2", re.compile(r"^\d{1,2}\s*\.\s*\d{1,2}\.?(?=\s)")),
    ("하이픈", re.compile(r"^\d{1,2}\s*-\s*\d{1,2}\s*[.)．]?(?=\s|[가-힣“\"'(A-Za-z])")),      # C2 ②
    ("질문", re.compile(r"^Q\s*\d{1,3}\s*[.:)．]?(?=\s|[가-힣“\"'(A-Za-z])")),                  # C2 ②
    ("숫자.", re.compile(r"^\d{1,2}(?:의\d{1,2})?\s*[.．](?=\s|[가-힣“\"'(A-Za-z])")),
    ("가.", re.compile(r"^[가-하]\s*[.．](?=\s|[가-힣“\"'(A-Za-z])")),
    ("숫자)", re.compile(r"^\d{1,2}\s*\)(?=\s|[가-힣“\"'(A-Za-z])")),
    ("가)", re.compile(r"^[가-하]\s*\)(?=\s|[가-힣“\"'(A-Za-z])")),
    ("(숫자)", re.compile(r"^\(\s*\d{1,2}\s*\)")),
    ("(가)", re.compile(r"^\(\s*[가-하]\s*\)")),
    ("원숫자", re.compile(r"^[①-⑳]")),
    ("원가", re.compile(r"^[㉮-㉻]")),
    ("서수", re.compile(r"^(?:첫|둘|셋|넷|다섯|여섯|일곱|여덟|아홉|열)째\s*[,.]")),
]
TOC_WORDS = {"목차", "차례", "contents", "tableofcontents"}                                       # C2 ①
LEADER = re.compile(r"(?:[·.․…‥ㆍ\-_]\s*){4,}\s*[-–(]?\s*\d{1,4}\s*[-–)]?\s*$")                   # C2 ①: "…… 12" 목차 줄
SENT_END_LINE = re.compile(r"(?:다|요|음|함|임|됨|것|등|바람|시오)\s*[.。]\s*$|[.?!。]\s*$")
SENT_SPLIT = re.compile(r"(?<=[.?!。])\s+|(?<=(?:다|요|음|함|임|됨)[.。])")
INTRO = re.compile(r"(?:[:：]\s*$|<\s*표\s*\d*[^>]*>\s*$|\[\s*표\s*\d*[^\]]*\]\s*$|\(\s*단위\s*[:：][^)]*\)\s*$|다음과\s*같(?:다|음)[.:]?\s*$)")


def family_of(text: str):
    t = text.lstrip()
    for name, rx in FAMILIES:
        m = rx.match(t)
        if m:
            return name, m.end()
    return None, 0


# ------------------------------------------------------------------ 1) 줄(조각) 만들기
@dataclass
class Piece:
    seg: Seg
    a: int                      # 항목 정규화 시작
    b: int                      # 끝
    text: str                   # 원문 조각
    head: str | None = None     # 제목이면 제목 종류
    level: int = 0
    path: list = field(default_factory=list)
    extra: list = field(default_factory=list)   # 제목에 붙인 꼬리 조각의 span (예: 부칙 뒤 "<법률 제…호>")
    toc_end: bool = False                       # C2 ①: 목차가 끝나고 본문이 시작되는 첫 조각

    @property
    def span(self):
        return (self.seg.idx, self.a, self.b)


def _norm_len(s: str) -> int:
    return len(norm(s))


def split_head(doc: Doc, seg: Seg, law: bool):
    """항목 앞머리의 조·장 제목을 떼어 [제목 조각, 본문 조각]으로. 법령형이 아니면 그대로."""
    raw = seg.raw
    t = raw.lstrip()
    lead = len(raw) - len(t)
    if law:
        m = JO.match(t)
        if m and m.end() < len(t.rstrip()):
            k = _norm_len(raw[: lead + m.end()])
            return [Piece(seg, 0, k, doc.raw_slice(seg, 0, k)), Piece(seg, k, seg.nlen, doc.raw_slice(seg, k, seg.nlen))]
    return [Piece(seg, 0, seg.nlen, seg.raw)]


def classify(doc: Doc, p: Piece, law: bool, is_whole_seg: bool):
    """조각이 제목이면 (종류, 법령 고정 단계 또는 None)."""
    t = p.text.strip()
    if not t:
        return None, None
    if norm(t).lower() in TOC_WORDS and len(t) <= 20:          # C2 ①
        return "목차", 1
    if p.seg.type == "document_index" or LEADER.search(t):    # C2 ①: 목차 줄은 제목 아님
        return None, None
    if SPECIAL.match(t) and len(t) <= 80:
        return "특별", 1
    if law:
        for name, lv, rx in LAW_HEAD:
            if rx.match(t) and len(t) <= 80:
                return name, lv
        if JO.match(t) and (len(t) <= 80 or not is_whole_seg):
            if _norm_len(JO.match(t).group(0)) >= _norm_len(t) - 1:
                return "조", 5
        if JO_BARE.match(t) or (is_whole_seg and len(t) <= 40 and JO_PLAIN.match(t)):
            return "조", 5
        if p.seg.type in ("title",):
            return "문서제목", 0
        return None, None
    fam, _ = family_of(t)
    # 11/20 dev 수정: 번호 줄은 모양(짧음·문장 아님)으로 정하지 않고 일단 후보로 둔다.
    # 실제 제목 여부는 role_filter()가 '밑에 딸린 글이 있나'로 정한다 (사람 결정 1과 같은 기준).
    if fam and len(t) <= 200:
        return fam, None
    if p.seg.type == "title":
        return "문서제목", 0
    if p.seg.type == "section_header" and not fam:
        return "상자제목", None
    return None, None


def role_filter(pieces: list[Piece], law: bool):
    """공문형 번호 제목 후보 중 밑에 딸린 것이 없는 줄은 본문으로 돌린다(역할 기준, 11/20 dev 수정).
    딸린 것 = 이 줄 다음부터 같은 단계 이하 제목이 나오기 전까지의 본문 조각 또는 더 깊은 제목.
    표 안 칸 글은 조각으로 나오지 않으므로, 바로 뒤가 표면 그 표가 딸린 것으로 본다."""
    if law:
        return False
    changed = False
    for k, p in enumerate(pieces):
        if not p.head or p.head in ("문서제목", "특별", "상자제목", "목차"):
            continue
        owns = False
        for q in pieces[k + 1:]:
            if q.head and q.head != "문서제목":
                owns = q.level > p.level and q.head not in ("특별",)
                break
            if q.b > q.a and (q.text.strip() or q.seg.type == "table"):
                owns = True
                break
        if not owns:
            p.head, p.level, p.path = None, 0, []
            changed = True
    return changed


def attach_tails(pieces: list[Piece]):
    """'부칙' 제목 바로 뒤의 짧은 '<법률 제…호, 날짜>' 줄을 제목에 붙인다(부칙이 여러 개일 때 구분, 11/20 dev 수정)."""
    out = []
    for p in pieces:
        if out and out[-1].head == "특별" and re.match(r"^부\s*칙\s*$", out[-1].text.strip()) and not p.head \
                and re.match(r"^\s*<[^>]{3,60}>\s*(\([^)]{1,30}\))?\s*$", p.text):
            h = out[-1]
            h.text = h.text.strip() + " " + p.text.strip()
            h.extra.append(p.span)
            continue
        out.append(p)
    return out


# ------------------------------------------------------------------ 2) 단계 매기기
def assign_levels(pieces: list[Piece], law: bool):
    stack: list[tuple[str, int, str]] = []     # (종류, 단계, 글)
    for p in pieces:
        if p.toc_end:                            # C2 ①: 본문 시작 → 목차 제목을 경로에서 닫는다
            fams = [f for f, _, _ in stack]
            if "목차" in fams:
                stack = stack[: fams.index("목차")]
        if not p.head:
            continue
        if p.head == "목차":                     # C2 ①: 목차는 맨 위 단계에서 시작
            stack = []
            p.level = 1
            stack.append(("목차", 1, p.text.strip()))
            p.path = [x[2] for x in stack]
            continue
        if p.head == "문서제목":
            p.level, p.path = 0, []
            continue
        if law or p.head == "특별":
            lv = p.level
            if p.head == "특별":
                stack = []
            while stack and stack[-1][1] >= lv:
                stack.pop()
        elif p.head == "상자제목":
            # 앞에 상자 제목이 이미 있으면 그 형제(그 아래 번호 제목들은 닫힘), 처음이면 지금 제목의 자식(함정 6). 11/20 dev 수정
            fams = [f for f, _, _ in stack]
            if "상자제목" in fams:
                stack = stack[: fams.index("상자제목")]
            lv = (stack[-1][1] + 1) if stack else 1
        else:
            fams = [f for f, _, _ in stack]
            if p.head in fams:                       # 같은 종류가 다시 나오면 형제
                k = fams.index(p.head)
                stack = stack[:k]
            lv = (stack[-1][1] + 1) if stack else 1
        p.level = lv
        stack.append((p.head, lv, p.text.strip()))
        p.path = [x[2] for x in stack]


# ------------------------------------------------------------------ 3) 단위
@dataclass
class Unit:
    head: Piece | None
    path: list
    body: list = field(default_factory=list)       # Piece 목록 (본문)


def build_units(doc: Doc, pieces: list[Piece]):
    units = [Unit(None, [])]
    for p in pieces:
        if p.head and p.head != "문서제목":
            units.append(Unit(p, list(p.path)))
        elif p.head == "문서제목":
            units[-1].body.append(p) if units[-1].head is not None else units[0].body.append(p)
        else:
            units[-1].body.append(p)
    # 내용 없는 제목: 다음 단위가 자식이면 경로로만, 아니면 제목 줄을 본문으로 넣어 글이 사라지지 않게 (함정 13)
    for k, u in enumerate(units):
        if u.head is None or u.body:
            continue
        nxt = units[k + 1] if k + 1 < len(units) else None
        is_parent = nxt is not None and nxt.head is not None and len(nxt.path) > len(u.path) and nxt.path[: len(u.path)] == u.path
        if not is_parent:
            u.body.append(Piece(u.head.seg, u.head.a, u.head.b, doc.raw_slice(u.head.seg, u.head.a, u.head.b)))
            for i, a, b in u.head.extra:                   # 제목에 붙였던 꼬리 줄도 본문으로 (글 빠짐 방지)
                sg = doc.by_idx[i]
                u.body.append(Piece(sg, a, b, doc.raw_slice(sg, a, b)))
            u.head = Piece(u.head.seg, u.head.a, u.head.a, "", head=u.head.head, level=u.head.level, path=u.head.path)
    return units


# ------------------------------------------------------------------ 표 글
def table_rows_md(seg: Seg, rows: list[int], hdr_rows: list[int]) -> str:
    g = seg.grid
    if not g:
        return ""
    nc = len(g[0])
    out = []
    show = hdr_rows + [r for r in rows if r not in hdr_rows]
    for k, r in enumerate(show):
        out.append("| " + " | ".join((g[r][c] or "").replace("\n", " ").replace("|", "/") for c in range(nc)) + " |")
        if k == len(hdr_rows) - 1 or (not hdr_rows and k == 0):
            out.append("|" + "---|" * nc)
    return "\n".join(out)


def table_rows_kv(seg: Seg, rows: list[int]) -> str:
    """검색용: 행마다 '열이름: 값'. 2열 이름표 표는 '왼쪽: 오른쪽' (함정 8). 병합 칸 값은 행마다 채움(격자 펼침)."""
    g = seg.grid
    if not g:
        return ""
    nr, nc = len(g), len(g[0])
    H = seg.header_rows
    left_short = nc == 2 and all(len((g[r][0] or "").strip()) <= 15 for r in range(nr))
    if left_short:
        return "\n".join(f"{(g[r][0] or '').strip()}: {(g[r][1] or '').strip()}" for r in rows if (g[r][0] or g[r][1]))
    names = []
    for c in range(nc):
        parts = []
        for r in range(H):
            v = (g[r][c] or "").strip()
            if v and v not in parts:
                parts.append(v)
        names.append(" ".join(parts) or f"열{c + 1}")
    out = []
    for r in rows:
        if r < H:
            continue
        cells = [f"{names[c]}: {(g[r][c] or '').strip()}" for c in range(nc) if (g[r][c] or "").strip()]
        if cells:
            out.append("; ".join(cells))
    return "\n".join(out)


# ------------------------------------------------------------------ 4) 나누기 재료(atom)
@dataclass
class Atom:
    spans: list
    content: str
    embed: str
    kind: str                     # 본문 / 목록 / 표
    hdr: dict = field(default_factory=dict)
    part: str = ""                # 큰 표를 나눴을 때 "1/3" 같은 순번
    splittable: str = ""          # 더 나눌 수 있는 방법


SPLIT_ORDER = ["원숫자", "숫자.", "가.", "숫자)", "가)", "(숫자)", "(가)"]


def _inner_marker_cuts(text: str, fam: str):
    """한 조각 안에서 같은 종류 번호가 줄 머리·띄어쓰기 뒤에 나오는 위치. 원숫자는 1씩 이어질 때만 (함정 4)."""
    pats = {
        "원숫자": r"(?:(?<=\s)|^)([①-⑳])(?=\s|[가-힣])",
        "숫자.": r"(?:(?<=\n)|(?<=\s))(\d{1,2}(?:의\d{1,2})?)\s*[.．](?=\s|[가-힣“\"'(])",
        "가.": r"(?:(?<=\n)|(?<=\s))([가-하])\s*[.．](?=\s)",
        "숫자)": r"(?:(?<=\n)|(?<=\s))(\d{1,2})\)(?=\s|[가-힣])",
        "가)": r"(?:(?<=\n)|(?<=\s))([가-하])\)(?=\s|[가-힣])",
        "(숫자)": r"(?:(?<=\n)|(?<=\s))\((\d{1,2})\)",
        "(가)": r"(?:(?<=\n)|(?<=\s))\(([가-하])\)",
    }
    cuts = []
    prev = None
    for m in re.finditer(pats[fam], text):
        if m.start() == 0:
            prev = m.group(1)
            continue
        after = text[m.end(): m.end() + 6]
        if re.match(r"\s*(?:에|의|을|를|과|와|부터|까지|에서|에\s*따)", after):   # "①에 따른" 같은 인용은 자르지 않음
            continue
        cur = m.group(1)
        if fam == "원숫자" and prev is not None and ord(cur) != ord(prev) + 1:
            continue
        cuts.append(m.start())
        prev = cur
    return cuts


def _sentence_cuts(text: str):
    return [m.end() for m in SENT_SPLIT.finditer(text) if 0 < m.end() < len(text.rstrip())]


def _char_cuts(text: str, budget: int):
    """C2 ③: 글자 단위로 budget 안에 드는 자리들(앞에서부터 욕심껏)."""
    cuts, start = [], 0
    while count_tokens(text[start:]) > budget:
        lo, hi = start + 1, len(text)
        while lo < hi:                       # budget 안에 드는 가장 긴 끝 찾기
            mid = (lo + hi + 1) // 2
            if count_tokens(text[start:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        if lo <= start:
            lo = start + 1
        cuts.append(lo)
        start = lo
    return cuts


def piece_atoms(doc: Doc, p: Piece, budget: int):
    """본문 조각 하나를 budget 이하 atom들로. 항 → 호 → 목 → 문장 → 띄어쓰기 순."""
    text = p.text
    whole = Atom([p.span], text.strip(), text.strip(), "목록" if p.seg.type == "list_item" else "본문")
    if count_tokens(text) <= budget:
        return [whole]
    for fam in SPLIT_ORDER + ["문장", "띄어쓰기", "글자"]:
        if fam == "문장":
            cuts = _sentence_cuts(text)
        elif fam == "글자":                   # C2 ③
            cuts = _char_cuts(text, budget)
        elif fam == "띄어쓰기":
            cuts = [m.start() for m in re.finditer(r"\s+", text)]
        else:
            cuts = _inner_marker_cuts(text, fam)
        if not cuts:
            continue
        parts, last = [], 0
        for c in cuts + [len(text)]:
            if c > last:
                parts.append((last, c))
                last = c
        # 조각들을 앞에서부터 budget 안으로 묶기
        out, cur = [], None
        for a, b in parts:
            if cur is None:
                cur = (a, b)
            elif count_tokens(text[cur[0]: b]) <= budget:
                cur = (cur[0], b)
            else:
                out.append(cur)
                cur = (a, b)
        if cur:
            out.append(cur)
        if len(out) <= 1:
            continue
        atoms = []
        for a, b in out:
            na = p.a + _norm_len(text[:a])
            nb = p.a + _norm_len(text[:b])
            if nb <= na:
                continue
            sub = Piece(p.seg, na, nb, doc.raw_slice(p.seg, na, nb))
            if count_tokens(sub.text) > budget and fam != "글자":
                atoms.extend(piece_atoms(doc, sub, budget))
            else:
                atoms.append(Atom([sub.span], sub.text.strip(), sub.text.strip(), whole.kind))
        return atoms
    return [whole]


def table_atoms(doc: Doc, seg: Seg, lead: list, budget: int):
    """표 하나(+앞 안내·캡션) → atom들. 크면 행 묶음 + 머리행 반복."""
    H = seg.header_rows
    rows = [r for r, a, b in seg.rows if b > a]
    lead_spans = [x.span for x in lead]
    lead_txt = "\n".join(x.text.strip() for x in lead if x.text.strip())

    def mk(rs, first):
        hdr = [r for r in range(H) if r in rows]
        md = table_rows_md(seg, rs, hdr)
        kv = table_rows_kv(seg, hdr + rs if first else rs)
        sp = ([] if not first else lead_spans) + [(seg.idx, a, b) for r, a, b in seg.rows if r in rs and b > a]
        if first:
            sp += [(seg.idx, a, b) for r, a, b in seg.rows if r in hdr and b > a]
        content = (lead_txt + "\n" if first and lead_txt else "") + md
        embed = (lead_txt + "\n" if first and lead_txt else "") + (kv or md)
        hdr_of = {} if first else ({str(seg.idx): len(hdr)} if hdr else {})
        return Atom(sorted(sp, key=lambda x: (x[0], x[1])), content, embed, "표", hdr_of)

    data = [r for r in rows if r >= H]
    full = mk(data, True)
    if count_tokens(full.embed) <= budget and count_tokens(full.content) <= budget:
        return [full]
    out, cur, first = [], [], True
    for r in data:
        trial = mk(cur + [r], first)
        if cur and (count_tokens(trial.embed) > budget or count_tokens(trial.content) > budget):
            out.append(mk(cur, first))
            first, cur = False, [r]
        else:
            cur = cur + [r]
    if cur or first:
        out.append(mk(cur, first))
    if len(out) > 1:
        for i, a in enumerate(out, 1):
            a.part = f"{i}/{len(out)}"
    return out


# ------------------------------------------------------------------ 본 청커
def chunk_structure(doc: Doc, max_tokens: int, fallback: bool = True, min_tokens: int = MIN_TOKENS):
    """fallback=False: 번호·제목이 없는 긴 단위를 길이로 나누지 않는다(회귀 확인용, 계획서 Q4)."""
    law = sum(1 for s in doc.segs if s.type != "table" and (JO.match(s.raw.lstrip() or "")
              or (len(s.raw.strip()) <= 40 and JO_PLAIN.match(s.raw.strip() or "")))) >= 3
    # 1) 조각
    pieces: list[Piece] = []
    for s in doc.segs:
        if s.nlen == 0:
            continue
        if s.type == "table":
            pieces.append(Piece(s, 0, s.nlen, s.raw))
            continue
        parts = split_head(doc, s, law)
        for k, p in enumerate(parts):
            if s.type in ("table", "picture", "footnote", "caption"):
                pieces.append(p)
                continue
            head, lv = classify(doc, p, law, is_whole_seg=len(parts) == 1)
            if head:
                p.head = head
                p.level = lv if lv is not None else 0
            pieces.append(p)
    # C2 ①: 목차 끝 표시 — 목차 제목 뒤로 목차 줄(document_index·점선 쪽번호 줄)·빈 조각이 이어지다 처음 나오는 다른 조각
    in_toc = False
    for p in pieces:
        t = p.text.strip()
        if p.head == "목차":
            in_toc = True
            continue
        if in_toc:
            if not t or p.seg.type == "document_index" or LEADER.search(t) or (p.seg.type == "table" and LEADER.search(p.seg.raw)):
                p.head, p.level = None, 0
                continue
            p.toc_end = True
            in_toc = False
    # 2) 단계 (부칙 꼬리 붙이기 → 단계 → 역할 기준으로 딸린 것 없는 번호 줄은 본문 → 단계 다시)
    pieces = attach_tails(pieces)
    assign_levels(pieces, law)
    if role_filter(pieces, law):
        assign_levels(pieces, law)
    # 3) 단위
    units = build_units(doc, pieces)
    title = doc.title
    chunks = []

    def prefix(path):
        return " > ".join([title] + [x for x in path if x]) + "\n"

    pending_heads: list[Piece] = []                  # 내용 없는 제목(경로로만 남는 것): 다음 청크에 글 위치를 붙인다
    for u in units:
        if not u.body:
            if u.head is not None and u.head.b > u.head.a:
                pending_heads.append(u.head)
            continue
        heads = pending_heads + ([u.head] if u.head is not None and u.head.b > u.head.a else [])
        pending_heads = []
        head_txt = "\n".join(h.text.strip() for h in heads if h.text.strip())
        pre = prefix(u.path)
        budget = max(32, max_tokens - count_tokens(pre) - count_tokens(head_txt) - 4)
        # 본문 → atom (표는 앞 안내 문장·캡션과 묶음)
        atoms, k = [], 0
        body = u.body
        while k < len(body):
            p = body[k]
            if p.seg.type == "table":
                lead = []
                if atoms and atoms[-1].kind != "표" and len(atoms[-1].spans) == 1:
                    prev_txt = atoms[-1].content
                    if len(prev_txt) <= 80 and INTRO.search(prev_txt):
                        prev_atom = atoms.pop()
                        sp = prev_atom.spans[0]
                        lead.append(Piece(doc.by_idx[sp[0]], sp[1], sp[2], prev_atom.content))
                caps = []
                while k + 1 < len(body) and body[k + 1].seg.type == "caption":
                    caps.append(body[k + 1])
                    k += 1
                atoms.extend(table_atoms(doc, p.seg, lead + caps, budget))
            elif p.seg.type == "list_item":
                grp = [p]
                while k + 1 < len(body) and body[k + 1].seg.type == "list_item" and not body[k + 1].head:
                    grp.append(body[k + 1])
                    k += 1
                txt = "\n".join(x.text.strip() for x in grp)
                if count_tokens(txt) <= budget:
                    atoms.append(Atom([x.span for x in grp], txt, txt, "목록"))
                else:
                    for x in grp:
                        atoms.extend(piece_atoms(doc, x, budget))
            else:
                if not fallback and not any(_inner_marker_cuts(p.text, f) for f in SPLIT_ORDER) and count_tokens(p.text) > budget:
                    atoms.append(Atom([p.span], p.text.strip(), p.text.strip(), "본문"))     # 회귀 확인용: 길이로 안 나눔
                else:
                    atoms.extend(piece_atoms(doc, p, budget))
            k += 1
        # atom 묶기 (같은 단위 안, budget 이하). 표는 따로 청크 (설계 4: 표 + 캡션 + 안내 = 한 청크)
        groups, cur = [], []
        for a in atoms:
            if a.kind == "표":
                if cur:
                    groups.append(cur)
                groups.append([a])
                cur = []
                continue
            if not cur:
                cur = [a]
                continue
            joined_e = "\n".join(x.embed for x in cur + [a])
            joined_c = "\n".join(x.content for x in cur + [a])
            if count_tokens(joined_e) <= budget and count_tokens(joined_c) <= budget:
                cur.append(a)
            else:
                groups.append(cur)
                cur = [a]
        if cur:
            groups.append(cur)
        for gi, g in enumerate(groups):
            spans = {tuple(sp) for a in g for sp in a.spans}
            body_c = "\n".join(a.content for a in g if a.content)
            body_e = "\n".join(a.embed for a in g if a.embed)
            if gi == 0 and heads:                         # 단위의 첫 청크에 제목 줄(원문)을 붙인다
                spans |= {h.span for h in heads} | {x for h in heads for x in h.extra}
                body_c = head_txt + ("\n" + body_c if body_c else "")
            hdr = {}
            for a in g:
                hdr.update(a.hdr)
            kinds = {a.kind for a in g}
            chunks.append({"spans": spans, "content": body_c, "ebody": body_e, "path": list(u.path),
                           "own": (u.head.text.strip() if u.head is not None and gi == 0 else ""),
                           "kind": kinds.pop() if len(kinds) == 1 else "섞임", "hdr": hdr, "unit": id(u), "first": gi == 0,
                           "part": g[0].part if len(g) == 1 else ""})
    if pending_heads and chunks:                          # 문서 끝의 내용 없는 제목
        chunks[-1]["spans"] |= {h.span for h in pending_heads} | {x for h in pending_heads for x in h.extra}
        chunks[-1]["content"] += "\n" + "\n".join(h.text.strip() for h in pending_heads)
    for c in chunks:
        c["embed"] = prefix(c["path"]) + c["ebody"]
    # 5) 짧으면 형제끼리 합치기: 같은 부모(부모가 1단계 이상), 새 청크가 min_tokens 미만이고 앞 청크도 짧은 것(또는 짧은 것끼리
    #    이미 합친 묶음)일 때만, 합쳐도 상한 이하, 표·나뉜 단위 조각 아님.
    #    (11/20 함정 단위 테스트에서 고침: 처음엔 '둘 중 하나가 짧으면'이라 긴 조에 짧은 조가 붙어 긴 조의 제목이 경로에서 사라졌다)
    #    합치면 경로는 부모까지만 → 빠지는 자기 제목은 본문 첫 줄로 남긴다(함정 14).
    merged = []
    for c in chunks:
        if merged:
            m = merged[-1]
            par = c["path"][:-1]
            ok = (m["path"][:-1] == par if not m.get("merged") else m["path"] == par)
            ok = ok and len(par) >= 1 and m["unit"] != c["unit"] and c["first"] and m["kind"] != "표" and c["kind"] != "표"
            ok = ok and count_tokens(c["embed"]) < min_tokens and (m.get("merged") or count_tokens(m["embed"]) < min_tokens)
            if ok:
                eb_m = m["ebody"] if m.get("merged") else ((m["own"] + "\n") if m["own"] else "") + m["ebody"]
                eb_c = ((c["own"] + "\n") if c["own"] else "") + c["ebody"]
                new_e = prefix(par) + eb_m + "\n" + eb_c
                new_c = m["content"] + "\n" + c["content"]
                if count_tokens(new_e) <= max_tokens and count_tokens(new_c) <= max_tokens:
                    m.update({"spans": m["spans"] | c["spans"], "content": new_c, "ebody": eb_m + "\n" + eb_c, "embed": new_e,
                              "path": par, "kind": m["kind"] if m["kind"] == c["kind"] else "섞임", "merged": True, "unit": c["unit"]})
                    continue
        merged.append(dict(c))
    out = []
    for k, c in enumerate(merged):
        sp = sorted(c["spans"], key=lambda x: (x[0], x[1]))
        pages = sorted({p for i, _, _ in sp for p in (doc.elements[i].get("pages") or [])})
        out.append({"chunk_index": k, "content": c["content"], "embed_text": c["embed"], "heading_path": list(c["path"]),
                    "kind": c["kind"], "page": pages[0] if pages else None, "pages": pages, "table_part": c.get("part", ""),
                    "source_spans": [list(x) for x in sp], "header_rows_of": c["hdr"]})
    return out
