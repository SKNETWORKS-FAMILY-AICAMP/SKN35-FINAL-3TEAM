# -*- coding: utf-8 -*-
"""스캔 쪽 그림에서 표 테두리(가로·세로 선)를 찾는다 → pdf_lines.PageLines와 같은 모양으로 돌려준다.

왜 필요한가
  글자층 PDF는 표 테두리가 선(도형)으로 들어 있어 pdf_lines로 읽는다. 스캔 PDF는 쪽 전체가 그림이라
  선 정보가 없다. 하지만 그림 속에는 테두리가 그대로 보이므로, 그림에서 긴 직선만 골라내면
  글자층 PDF와 같은 '선 기준' 표 보정(병합 칸·열 맞추기)을 스캔에도 쓸 수 있다.

방법 (OpenCV 형태학 연산)
  1) 쪽을 RENDER_DPI로 그린다(pypdfium2 — Docling이 쓰는 것과 같은 렌더러).
  2) 흑백 반전 + 적응 이진화 → 어두운 획이 1.
  3) 가로로 긴 막대(MIN_LEN pt)로 열림(open) 연산 → 가로로 길게 이어진 획만 남는다(글자 획은 짧아서 사라짐).
     세로도 같은 방식.
  4) 남은 덩어리마다 감싸는 사각형을 구하고, 두께가 MAX_THICK pt 이하면 선으로 본다.
  5) 픽셀 → pt(1/72인치), 왼쪽 위 원점으로 바꾼다.
"""
from __future__ import annotations

import io

from agent_platform.knowledge.ingestion.parsers.pdf.pdf_lines import PageLines

RENDER_DPI = 200       # 그림 해상도 (스캔 원본이 보통 200~300dpi)
MIN_LEN = 12.0         # 이보다 짧은 직선은 글자 획으로 보고 버린다 (pt) — 가장 좁은 칸 폭보다 작게
MAX_THICK = 4.0        # 이보다 두꺼우면 선이 아니라 칠한 영역 (pt)
GAP_CLOSE = 2.0        # 스캔으로 끊긴 선을 이 길이(pt)까지 이어 붙인다


def _lines_from_image(gray, scale: float, height_pt: float) -> PageLines:
    import cv2
    import numpy as np

    bw = cv2.adaptiveThreshold(255 - gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -10)
    pl = PageLines(height=height_pt)
    k_len = max(3, int(MIN_LEN * scale))
    k_gap = max(1, int(GAP_CLOSE * scale))
    for horizontal in (True, False):
        # 끊긴 선 잇기(닫힘) → 긴 막대로 열림: 긴 직선만 남는다
        close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (k_gap, 1) if horizontal else (1, k_gap))
        open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (k_len, 1) if horizontal else (1, k_len))
        m = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, close_k)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, open_k)
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        for i in range(1, n):
            x, y, w, h, _area = stats[i]
            # numpy 수 → 파이썬 float (numpy bool이 섞이면 `is False` 판단이 틀어진다)
            x0, y0, x1, y1 = float(x) / scale, float(y) / scale, float(x + w) / scale, float(y + h) / scale
            if horizontal and (y1 - y0) <= MAX_THICK and (x1 - x0) >= MIN_LEN:
                pl.h.append(((y0 + y1) / 2, x0, x1))
            elif not horizontal and (x1 - x0) <= MAX_THICK and (y1 - y0) >= MIN_LEN:
                pl.v.append(((x0 + x1) / 2, y0, y1))
    del np
    return pl


def read_image_lines(pdf_bytes: bytes, pages: set[int] | None = None) -> dict[int, PageLines]:
    """PDF 바이트 → 쪽 번호별 '그림에서 찾은 선'. pages를 주면 그 쪽만 (1부터)."""
    import numpy as np
    import pypdfium2 as pdfium

    out: dict[int, PageLines] = {}
    pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    try:
        scale = RENDER_DPI / 72.0
        for i in range(len(pdf)):
            no = i + 1
            if pages is not None and no not in pages:
                continue
            page = pdf[i]
            img = page.render(scale=scale, grayscale=True).to_pil()
            gray = np.asarray(img.convert("L"))
            out[no] = _lines_from_image(gray, scale, page.get_height())
            page.close()
    finally:
        pdf.close()
    return out


def read_lines(pdf_bytes: bytes) -> dict[int, PageLines]:
    """표 보정용 선: 쪽마다 글자층 선을 쓰고, 글자층 선이 하나도 없는 쪽(스캔)은 그림에서 찾는다."""
    from agent_platform.knowledge.ingestion.parsers.pdf.pdf_lines import read_pdf_lines

    vec = read_pdf_lines(pdf_bytes)
    empty = {no for no, pl in vec.items() if not pl.h and not pl.v}
    if empty:
        vec.update(read_image_lines(pdf_bytes, empty))
    return vec
