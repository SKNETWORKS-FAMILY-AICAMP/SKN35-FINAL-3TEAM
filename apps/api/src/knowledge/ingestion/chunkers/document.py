# -*- coding: utf-8 -*-
"""파서 결과(dict) → 청커가 읽는 '글 흐름'. (2차 청킹 실험 chunkexp/doc.py에서 옮김)

좌표계 (모든 방식을 같은 잣대로 재기 위해)
  - 항목(element)마다 원문 글(raw)을 만든다.
      글 항목: marker(목록 번호)가 있으면 "marker 글", 없으면 글 그대로
      표: 행마다 칸 글을 공백으로 잇고, 행끼리는 줄바꿈 (병합 칸은 기준 칸에서 한 번만)
  - 정규화 = 공백 문자를 모두 뺀 글. 문서 전체 정규화 글 G = 항목 정규화 글을 순서대로 이은 것.
  - 청크는 "덮은 범위" 목록(spans) = [(항목 번호, 정규화 시작, 정규화 끝)]로 나타낸다.
    표는 행 경계에서만 나누므로 행 범위가 그대로 정규화 범위가 된다.
청킹 방식이 무엇이든 spans만 맞으면 글 보존·중복·경계 위치를 같은 방법으로 잴 수 있다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_TYPES = {"page_header", "page_footer"}          # 머리말·꼬리말 (파서가 body에 두지 않지만 혹시 대비)
HEADING_TYPES = {"title", "section_header"}
WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return WS.sub("", s or "")


@dataclass
class Seg:
    idx: int                 # elements 안 번호
    type: str
    raw: str                 # 원문 글 (위 규칙)
    g0: int                  # 문서 정규화 글 G에서 시작
    g1: int                  # 끝 (배타)
    n2r: list = field(repr=False, default_factory=list)   # 정규화 위치 → raw 위치
    rows: list = field(default_factory=list)              # 표: [(행 번호, 정규화 시작, 끝)] (항목 기준 좌표)
    header_rows: int = 0     # 표: 머리행 수 (0행 칸들의 행 병합 최댓값)
    grid: list = field(repr=False, default_factory=list)  # 표: 펼친 격자 [행][열] 글 (병합 칸은 값 반복)

    @property
    def nlen(self) -> int:
        return self.g1 - self.g0


def _table_raw(el: dict):
    t = el["table"]
    nr, nc = t["rows"], t["cols"]
    grid = [["" for _ in range(nc)] for _ in range(nr)]
    anchors = [[] for _ in range(nr)]
    for c in sorted(t["cells"], key=lambda x: (x["r"], x["c"])):
        anchors[c["r"]].append(c["text"] or "")
        for rr in range(c["r"], min(nr, c["r"] + max(1, c["rs"]))):
            for cc in range(c["c"], min(nc, c["c"] + max(1, c["cs"]))):
                grid[rr][cc] = c["text"] or ""
    hdr = max([max(1, c["rs"]) for c in t["cells"] if c["r"] == 0] or [1]) if nr > 1 else 1
    row_texts = [" ".join(x for x in a if x) for a in anchors]
    return row_texts, grid, min(hdr, nr)


class Doc:
    def __init__(self, parsed: dict | str | Path, name: str | None = None):
        """parsed: 파서 결과 dict(ParseResult.to_dict()) 또는 그 JSON 파일 경로."""
        if isinstance(parsed, dict):
            self.path = None
            self.d = parsed
            self.name = name or (parsed.get("file_name") or "")
        else:
            self.path = Path(parsed)
            self.d = json.loads(self.path.read_text(encoding="utf-8"))
            self.name = name or self.path.name[:-5]           # "규정.hwpx.json" → "규정.hwpx"
        self.elements = self.d["elements"]
        titles = [e for e in self.elements if e["type"] == "title" and e.get("text")]
        self.title = titles[0]["text"].strip() if titles else Path(self.d.get("file_name") or self.name).stem
        self.segs: list[Seg] = []
        g = []
        pos = 0
        for i, el in enumerate(self.elements):
            if el["type"] in SKIP_TYPES:
                continue
            rows, grid, hdr = [], [], 0
            if el["type"] == "table" and el.get("table"):
                row_texts, grid, hdr = _table_raw(el)
                raw = "\n".join(row_texts)
                p = 0
                for r, rt in enumerate(row_texts):
                    n = len(norm(rt))
                    rows.append((r, p, p + n))
                    p += n
            else:
                txt = el.get("text") or ""
                mk = (el.get("marker") or "").strip()
                raw = f"{mk} {txt}" if mk else txt
            n2r = [k for k, ch in enumerate(raw) if not ch.isspace()]
            n = len(n2r)
            self.segs.append(Seg(i, el["type"], raw, pos, pos + n, n2r, rows, hdr, grid))
            g.append(norm(raw))
            pos += n
        self.G = "".join(g)
        self.by_idx = {s.idx: s for s in self.segs}
        # 구조 경계 위치 (G 좌표): 항목 시작, 표 행 시작, 항목 안 번호 머리(①, 1., 가., (1), 1) …)
        self.elem_starts = {s.g0 for s in self.segs if s.nlen}
        self.row_starts = {s.g0 + a for s in self.segs for _, a, _ in s.rows}
        self.marker_starts = set()
        mk = re.compile(r"(?:^|(?<=\s))(?:[①-⑳]|\d{1,2}\.(?!\d)|[가-하]\.|\(\d{1,2}\)|\d{1,2}\)|[가-하]\))")
        for s in self.segs:
            if s.type == "table":
                continue
            for m in mk.finditer(s.raw):
                if m.start() == 0:
                    continue
                k = sum(1 for ch in s.raw[:m.start()] if not ch.isspace())
                if 0 < k < s.nlen:
                    self.marker_starts.add(s.g0 + k)
        self.struct_starts = self.elem_starts | self.row_starts | self.marker_starts

    # ---- 좌표 바꾸기 ----
    def raw_slice(self, seg: Seg, a: int, b: int) -> str:
        """항목 정규화 범위 [a,b) → 원문 글 조각."""
        if a >= b:
            return ""
        return seg.raw[seg.n2r[a]: seg.n2r[b - 1] + 1]

    def raw_before_char(self, gpos: int) -> str:
        """G 위치 바로 앞의 원문 글자 (같은 항목 안). 항목 시작이면 ''."""
        for s in self.segs:
            if s.g0 < gpos < s.g1:
                k = gpos - s.g0
                return s.raw[s.n2r[k - 1]]
        return ""

    def global_spans(self, spans) -> list[tuple[int, int]]:
        out = []
        for i, a, b in spans:
            s = self.by_idx[i]
            if b > a:
                out.append((s.g0 + a, s.g0 + b))
        return out
