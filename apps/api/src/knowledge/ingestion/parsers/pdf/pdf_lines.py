# -*- coding: utf-8 -*-
"""PDF 글자층에서 표 테두리(가로·세로 선)를 꺼낸다.

왜 필요한가
  Docling 표 칸의 bbox는 '글자에 딱 붙은 크기'라서 열 경계를 모른다.
  글자층 PDF에는 표 테두리가 실제 선(도형)으로 들어 있으므로, 그 x 좌표를 열 경계로 쓸 수 있다.
  스캔 PDF는 선이 그림 속에 있어 여기서는 아무것도 안 나온다 → 쓰는 쪽에서 글자 위치로 대체한다.

방법
  docling-parse가 주는 도형(shape)은 점 목록이다. 도형을 감싸는 사각형이
  - 높이가 1.5pt 이하이고 가로로 3pt 넘게 길면 가로선,
  - 폭이 1.5pt 이하이고 세로로 3pt 넘게 길면 세로선으로 본다.
  HWP는 테두리를 선으로, Word는 얇은 채운 사각형으로 그리는데 둘 다 이 규칙에 걸린다.
  좌표는 Docling 결과와 같게 '왼쪽 위 원점'(y가 아래로 커짐)으로 바꾼다.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

THIN = 1.5          # 이 폭 이하인 도형을 선으로 본다 (pt)
MIN_LEN = 3.0       # 이보다 짧은 변은 글자 장식·점으로 보고 버린다 (pt)
DASH_GAP = 6.0      # 점선: 같은 높이(너비)의 짧은 조각 사이 틈이 이 이하이면 한 줄로 잇는다 (pt)
DASH_MIN_PIECES = 4  # 점선으로 볼 최소 조각 수
DASH_MIN_SPAN = 20.0  # 점선으로 볼 최소 길이 (pt)


@dataclass
class PageLines:
    """한 쪽의 선. h: (y, x0, x1), v: (x, y0, y1) — 왼쪽 위 원점."""
    height: float
    h: list[tuple[float, float, float]] = field(default_factory=list)
    v: list[tuple[float, float, float]] = field(default_factory=list)

    def verticals_in(self, l: float, t: float, r: float, b: float, min_cover: float = 0.3) -> list[float]:
        """영역 (l,t,r,b) 안의 세로선 x 좌표 (2pt 안은 하나로 묶음).

        영역 높이의 min_cover 이상을 덮는 x만 남긴다 → 칸 안 장식 선은 빠지고 열 경계만 남는다.
        """
        cover: dict[float, float] = {}
        for x, y0, y1 in self.v:
            if not (l - 2 <= x <= r + 2):
                continue
            ov = min(y1, b) - max(y0, t)
            if ov > 0:
                cover[x] = cover.get(x, 0.0) + ov
        xs = sorted(x for x, c in cover.items() if c >= min_cover * (b - t))
        out: list[float] = []
        for x in xs:                                   # 이중선·두께 때문에 가까운 x를 하나로
            if out and x - out[-1] <= 2.0:
                continue
            out.append(x)
        return out


def _chain_dashes(dashes, horizontal: bool):
    """짧은 조각(점선의 점·대시)을 같은 줄끼리 이어 선으로 만든다.

    왜: 행 사이를 점선으로 긋는 표(통계표에 흔함)는 조각 하나하나가 MIN_LEN보다 짧아 선으로 안 잡혔고,
        그 결과 '행 사이에 선이 없다 → 줄바꿈된 한 행'으로 오판해 진짜 행을 합친 일이 있었다(시험셋 v2 로그).
    방법: 가로선이면 조각 가운데 y가 0.6pt 안으로 같은 것끼리 x 순서로 늘어놓고, 틈이 DASH_GAP 이하면 잇는다.
          DASH_MIN_PIECES개 이상, DASH_MIN_SPAN pt 이상 이어지면 선 하나로 본다.
    """
    keyf = (lambda d: (d[1] + d[3]) / 2) if horizontal else (lambda d: (d[0] + d[2]) / 2)
    startf = (lambda d: d[0]) if horizontal else (lambda d: d[1])
    endf = (lambda d: d[2]) if horizontal else (lambda d: d[3])
    rows: list[list] = []
    for d in sorted(dashes, key=keyf):
        if rows and abs(keyf(rows[-1][0]) - keyf(d)) <= 0.6:
            rows[-1].append(d)
        else:
            rows.append([d])
    out = []
    for row in rows:
        row.sort(key=startf)
        run = [row[0]]
        for d in row[1:] + [None]:
            if d is not None and startf(d) - endf(run[-1]) <= DASH_GAP:
                run.append(d)
                continue
            a, b = startf(run[0]), max(endf(x) for x in run)
            if len(run) >= DASH_MIN_PIECES and b - a >= DASH_MIN_SPAN:
                out.append((sum(keyf(x) for x in run) / len(run), a, b))
            if d is not None:
                run = [d]
    return out


def read_pdf_lines(pdf_bytes: bytes) -> dict[int, PageLines]:
    """PDF 바이트 → 쪽 번호별 선. (docling-parse는 한글 경로를 못 열어서 바이트로 받는다.)"""
    from docling_parse.pdf_parser import DoclingPdfParser

    pdf = DoclingPdfParser().load(path_or_stream=io.BytesIO(pdf_bytes))
    out: dict[int, PageLines] = {}
    try:
        for no, page in pdf.iterate_pages():
            H = page.dimension.height
            pl = PageLines(height=H)
            dashes: list[tuple[float, float, float, float]] = []
            for s in page.shapes:
                if not s.points:
                    continue
                xs = [p.x for p in s.points]
                ys = [H - p.y for p in s.points]             # 왼쪽 아래 원점 → 왼쪽 위 원점
                w, h = max(xs) - min(xs), max(ys) - min(ys)
                # 선처럼 생긴 도형만 쓴다: 한쪽 폭이 THIN 이하인 것(그은 선, 또는 Word의 얇은 채운 사각형).
                # 큰 사각형(칸 음영·글자 배경)의 변은 테두리가 아니라서 버린다
                # — S1 Word PDF에서 칸 안쪽 여백 사각형이 가짜 '열 경계'를 만들었다.
                if w <= THIN and h > MIN_LEN:
                    pl.v.append(((min(xs) + max(xs)) / 2, min(ys), max(ys)))
                elif h <= THIN and w > MIN_LEN:
                    pl.h.append(((min(ys) + max(ys)) / 2, min(xs), max(xs)))
                elif max(w, h) <= MIN_LEN:
                    dashes.append((min(xs), min(ys), max(xs), max(ys)))   # 점선 조각·점 후보
            pl.h += _chain_dashes(dashes, horizontal=True)
            pl.v += _chain_dashes(dashes, horizontal=False)
            out[no] = pl
    finally:
        pdf.unload()
    return out


def read_pdf_words(pdf_bytes: bytes) -> dict[int, list[tuple[str, float, float, float, float]]]:
    """PDF 글자층 낱말 → 쪽 번호별 [(글자, l, t, r, b)] (왼쪽 위 원점). 스캔 쪽은 빈 목록.

    표 모델이 두 열을 한 칸으로 읽었을 때 낱말 위치로 칸을 다시 나누는 데 쓴다.
    """
    from docling_core.types.doc import CoordOrigin
    from docling_parse.pdf_parser import DoclingPdfParser

    pdf = DoclingPdfParser().load(path_or_stream=io.BytesIO(pdf_bytes))
    out: dict[int, list[tuple[str, float, float, float, float]]] = {}
    try:
        for no, page in pdf.iterate_pages():
            H = page.dimension.height
            ws = []
            for w in page.word_cells:
                bb = w.rect.to_bounding_box()
                if bb.coord_origin != CoordOrigin.TOPLEFT:
                    bb = bb.to_top_left_origin(H)
                if w.text.strip():
                    ws.append((w.text, bb.l, bb.t, bb.r, bb.b))
            out[no] = ws
    finally:
        pdf.unload()
    return out
