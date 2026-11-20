# -*- coding: utf-8 -*-
"""표 구조 인식기의 약속(인터페이스). 뼈대는 같고 인식기만 갈아끼운다.

입력  TableRegion : 표 하나의 자리(쪽 좌표, 왼쪽 위 원점 pt), 그 안 낱말(글자층), 선(있으면), 쪽 그림(필요한 인식기만)
출력  Grid        : 행 수·열 수·칸 목록(시작 행·끝 행·시작 열·끝 열, 칸 위치 pt). 글자는 넣지 않는다 —
                    칸 글자 채우기는 모든 인식기가 같은 공통 단계(fill.py)를 써서 비교가 공정하다.
인식 못 하면 None → 호출하는 쪽이 Docling 기본 결과(TableFormer)를 그대로 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TableRegion:
    page_no: int
    bbox: tuple[float, float, float, float]            # l, t, r, b (pt, 왼쪽 위 원점)
    words: list[tuple[str, float, float, float, float]]  # (글자, l, t, r, b) pt
    lines: object | None = None                        # pdf_lines.PageLines (쪽 전체)
    image: object | None = None                        # PIL 그림: 표 영역만 잘라 낸 것
    px2pt: float = 1.0                                 # 그림 1픽셀 = px2pt pt (image가 있을 때)


@dataclass
class Grid:
    num_rows: int
    num_cols: int
    cells: list[tuple[int, int, int, int, tuple[float, float, float, float]]] = field(default_factory=list)
    # (r0, r1, c0, c1, (l, t, r, b) pt) — r1·c1은 끝 다음 번호


class TableBackend(Protocol):
    name: str
    needs_image: bool

    def recognize(self, region: TableRegion) -> Grid | None: ...
