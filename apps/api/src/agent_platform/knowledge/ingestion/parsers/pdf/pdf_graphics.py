# -*- coding: utf-8 -*-
"""PDF 내용 스트림에서 표 판정에 쓰는 그래픽을 모은다: 칸 클립 사각형, 획 괘선, 채움 사각형 (pypdf).

왜 pypdf인가
  한컴 PDF는 표 칸마다 '경로 + W* n'(잘라내기) 사각형을 깔고 그 안에 글을 찍는다. 이 사각형이 원본 칸 경계 그대로라
  병합 칸·보이지 않는 칸 쪼개기까지 담는다(clip_tables.py). pypdfium2의 FPDFPageObj_GetClipPath로는 이 클립이 안 나와
  (실측 0개) 내용 스트림 연산자를 직접 따라간다.

좌표: PDF 사용자 공간(왼쪽 아래 원점, y는 위로) — kordoc(pdf.js)과 같은 좌표계라 이식한 판정 상수를 그대로 쓴다.
따라가는 것: q/Q(그래픽 상태 저장), cm(변환 행렬), m/l/h/re/c/v/y(경로), S/s/f/F/f*/B/B*/b/b*/n(칠하기·끝),
  W/W*(다음 칠하기에서 클립), Do(폼 XObject 안으로 — /Matrix 적용), w(선 두께).
원본: kordoc src/pdf/line-extract.ts extractLines (MIT, Copyright 2026 chrisryugj) 의 클립·괘선·채움 수집 부분을
  pypdf로 다시 작성. 색·투명도·흰 획 구분 같은 세부 판정은 옮기지 않았다(칸 클립 격자 판정에 필요한 만큼만).
"""
from __future__ import annotations

from dataclasses import dataclass, field

ORIENTATION_TOL = 1.0       # 이 안이면 수평/수직 선 (pt) — kordoc ORIENTATION_TOL
THIN_FILL_MAX = 1.5         # 짧은 변이 이 이하인 채움 사각형은 괘선 (pt)
MIN_LINE_LENGTH = 3.0       # 이보다 짧은 획 조각은 버린다 (pt)
CLIP_MIN_W, CLIP_MIN_H = 4.0, 2.0


@dataclass
class Seg:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class Rect:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class PageGraphics:
    width: float
    height: float
    clips: list[Rect] = field(default_factory=list)
    horizontals: list[Seg] = field(default_factory=list)   # y1 == y2, x1 < x2
    verticals: list[Seg] = field(default_factory=list)     # x1 == x2, y1 < y2
    fills: list[Rect] = field(default_factory=list)
    short_h: list[Seg] = field(default_factory=list)       # MIN_LINE_LENGTH 미만 획 조각 — 칸 보이는 변 판정(cell-edges)용
    short_v: list[Seg] = field(default_factory=list)
    producer: str = ""                                      # 문서 Producer/Creator — 한컴 PDF 판별


def _mul(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _rect_of(path: list[Seg], min_w: float, min_h: float) -> Rect | None:
    """경로가 축 정렬 사각형(3~5 세그먼트, 전부 수평/수직)이면 bbox — kordoc captureClipRect."""
    if not 3 <= len(path) <= 5:
        return None
    for s in path:
        if abs(s.x2 - s.x1) > ORIENTATION_TOL and abs(s.y2 - s.y1) > ORIENTATION_TOL:
            return None
    x1 = min(min(s.x1, s.x2) for s in path); x2 = max(max(s.x1, s.x2) for s in path)
    y1 = min(min(s.y1, s.y2) for s in path); y2 = max(max(s.y1, s.y2) for s in path)
    if x2 - x1 < min_w or y2 - y1 < min_h:
        return None
    return Rect(x1, y1, x2, y2)


def _add_line(s: Seg, g: PageGraphics) -> None:
    dx, dy = abs(s.x2 - s.x1), abs(s.y2 - s.y1)
    if dy <= ORIENTATION_TOL and dx > 0:
        y = (s.y1 + s.y2) / 2
        seg = Seg(min(s.x1, s.x2), y, max(s.x1, s.x2), y)
        (g.horizontals if dx >= MIN_LINE_LENGTH else g.short_h).append(seg)
    elif dx <= ORIENTATION_TOL and dy > 0:
        x = (s.x1 + s.x2) / 2
        seg = Seg(x, min(s.y1, s.y2), x, max(s.y1, s.y2))
        (g.verticals if dy >= MIN_LINE_LENGTH else g.short_v).append(seg)


def _walk(content, resources, ctm, g: PageGraphics, depth=0) -> None:
    from pypdf.generic import ContentStream

    if depth > 4 or content is None:
        return
    stack: list[list[float]] = []
    path: list[Seg] = []
    rects: list[Rect] = []                 # 이번 경로의 re 사각형 (원래 크기 — 얇은 사각형 판정용)
    start = cur = (0.0, 0.0)
    pending_clip = False

    def T(x, y):
        return (x * ctm[0] + y * ctm[2] + ctm[4], x * ctm[1] + y * ctm[3] + ctm[5])

    def flush(stroke: bool, fill: bool):
        nonlocal path, rects, pending_clip
        if fill:
            if rects:
                g.fills.extend(rects)
            else:
                r = _rect_of(path, 0.3, 0.3)
                if r:
                    g.fills.append(r)
            # 얇은 채움 사각형은 괘선으로 그은 것이다
            for r in rects or ([_rect_of(path, 0.3, 0.3)] if path else []):
                if r is None:
                    continue
                if r.y2 - r.y1 <= THIN_FILL_MAX < r.x2 - r.x1:
                    _add_line(Seg(r.x1, (r.y1 + r.y2) / 2, r.x2, (r.y1 + r.y2) / 2), g)
                elif r.x2 - r.x1 <= THIN_FILL_MAX < r.y2 - r.y1:
                    _add_line(Seg((r.x1 + r.x2) / 2, r.y1, (r.x1 + r.x2) / 2, r.y2), g)
        if stroke:
            for s in path:
                _add_line(s, g)
        if not stroke and not fill and pending_clip:
            r = _rect_of(path, CLIP_MIN_W, CLIP_MIN_H)
            if r:
                g.clips.append(r)
        path, rects, pending_clip = [], [], False

    try:
        ops = ContentStream(content, None).operations
    except Exception:
        return
    for operands, op in ops:
        op = op.decode("latin-1") if isinstance(op, bytes) else op
        try:
            if op == "q":
                stack.append(ctm[:])
            elif op == "Q":
                ctm = stack.pop() if stack else ctm
            elif op == "cm":
                ctm = _mul([float(v) for v in operands], ctm)
            elif op == "m":
                cur = start = T(float(operands[0]), float(operands[1]))
            elif op == "l":
                p = T(float(operands[0]), float(operands[1]))
                path.append(Seg(cur[0], cur[1], p[0], p[1])); cur = p
            elif op in ("c", "v", "y"):
                p = T(float(operands[-2]), float(operands[-1]))
                path.append(Seg(cur[0], cur[1], p[0], p[1])); path.append(Seg(0, 0, 1e6, 1e6))   # 곡선: 사각형 아님 표시
                cur = p
            elif op == "h":
                if cur != start:
                    path.append(Seg(cur[0], cur[1], start[0], start[1]))
                cur = start
            elif op == "re":
                x, y, w, hh = (float(v) for v in operands)
                a, b = T(x, y), T(x + w, y + hh)
                r = Rect(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
                rects.append(r)
                if r.y2 - r.y1 < ORIENTATION_TOL * 2:
                    path.append(Seg(r.x1, (r.y1 + r.y2) / 2, r.x2, (r.y1 + r.y2) / 2))
                elif r.x2 - r.x1 < ORIENTATION_TOL * 2:
                    path.append(Seg((r.x1 + r.x2) / 2, r.y1, (r.x1 + r.x2) / 2, r.y2))
                else:
                    path += [Seg(r.x1, r.y1, r.x2, r.y1), Seg(r.x2, r.y1, r.x2, r.y2), Seg(r.x2, r.y2, r.x1, r.y2), Seg(r.x1, r.y2, r.x1, r.y1)]
                cur = start = T(x, y)
            elif op in ("W", "W*"):
                pending_clip = True
            elif op == "n":
                flush(False, False)
            elif op in ("S",):
                flush(True, False)
            elif op == "s":
                if cur != start:
                    path.append(Seg(cur[0], cur[1], start[0], start[1]))
                flush(True, False)
            elif op in ("f", "F", "f*"):
                flush(False, True)
            elif op in ("B", "B*", "b", "b*"):
                flush(True, True)
            elif op == "Do" and resources is not None:
                xo = (resources.get("/XObject") or {})
                xo = xo.get_object() if hasattr(xo, "get_object") else xo
                obj = xo.get(operands[0]) if xo else None
                obj = obj.get_object() if obj is not None else None
                if obj is not None and obj.get("/Subtype") == "/Form":
                    m = [float(v) for v in obj.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                    _walk(obj, obj.get("/Resources") or resources, _mul(m, ctm), g, depth + 1)
        except (ValueError, IndexError, TypeError):
            continue


def read_page_graphics(pdf_path) -> dict[int, PageGraphics]:
    """PDF 파일 → {쪽 번호(1부터): PageGraphics}."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    try:
        meta = reader.metadata or {}
        producer = " | ".join(str(meta.get(k) or "") for k in ("/Producer", "/Creator"))
    except Exception:
        producer = ""
    out: dict[int, PageGraphics] = {}
    for i, page in enumerate(reader.pages):
        mb = page.mediabox
        g = PageGraphics(width=float(mb.width), height=float(mb.height), producer=producer)
        ox, oy = float(mb.left), float(mb.bottom)
        ctm = [1, 0, 0, 1, -ox, -oy]                    # 쪽 상자 원점이 0이 아닌 PDF 대비
        try:
            content = page.get_contents()
        except Exception:
            content = None
        res = page.get("/Resources")
        _walk(content, res.get_object() if res is not None else None, ctm, g)
        out[i + 1] = g
    return out
