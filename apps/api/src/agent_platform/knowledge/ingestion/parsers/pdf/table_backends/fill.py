# -*- coding: utf-8 -*-
"""공통 단계: Grid + 낱말 → Docling TableData (모든 인식기가 같은 방법으로 칸 글자를 채운다)."""
from __future__ import annotations

from docling_core.types.doc import BoundingBox, CoordOrigin, TableCell, TableData

from agent_platform.knowledge.ingestion.parsers.pdf.table_backends.base import Grid


LINE_OVERLAP = 0.5    # 두 낱말의 세로 겹침이 낮은 쪽 높이의 이 비율 이상이면 같은 줄


def order_words(words) -> list:
    """word_lines를 이어 붙인 한 줄 목록."""
    return [w for ln in word_lines(words) for w in ln]


def word_lines(words) -> list[list]:
    """칸 안 낱말 [(글자, l, t, r, b)] → 읽는 순서 (줄은 위→아래, 줄 안은 왼→오른).

    줄 묶기는 세로로 겹치는 정도로 한다. 예전 방식(가운데 y ÷ 글자 높이를 반올림)은 같은 줄 낱말이
    반올림 경계에 걸리면 다른 줄로 갈라져 '중량물 취급작업은'이 '취급작업은 중량물'로 뒤집혔다
    (시험셋 v2 작업장 점검표, 칸 단위 채점 score_cells로 발견)."""
    lines: list[list] = []
    for w in sorted(words, key=lambda w: (w[2] + w[4]) / 2):
        if lines:
            cur = lines[-1]
            t, b = min(x[2] for x in cur), max(x[4] for x in cur)
            ov = min(b, w[4]) - max(t, w[2])
            if ov >= LINE_OVERLAP * min(b - t, w[4] - w[2]):
                cur.append(w)
                continue
        lines.append([w])
    return [sorted(ln, key=lambda w: w[1]) for ln in lines]


def grid_to_tabledata(grid: Grid, words) -> TableData:
    """낱말 가운데가 칸 안에 있으면 그 칸 글자. 칸 안 순서는 order_words (줄 위→아래, 줄 안 왼→오른)."""
    out = []
    for r0, r1, c0, c1, (l, t, r, b) in grid.cells:
        inside = order_words([w for w in words if l <= (w[1] + w[3]) / 2 <= r and t <= (w[2] + w[4]) / 2 <= b])
        out.append(TableCell(start_row_offset_idx=r0, end_row_offset_idx=r1, start_col_offset_idx=c0, end_col_offset_idx=c1,
                             row_span=r1 - r0, col_span=c1 - c0, text=" ".join(w[0] for w in inside),
                             bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT)))
    return TableData(num_rows=grid.num_rows, num_cols=grid.num_cols, table_cells=out)
