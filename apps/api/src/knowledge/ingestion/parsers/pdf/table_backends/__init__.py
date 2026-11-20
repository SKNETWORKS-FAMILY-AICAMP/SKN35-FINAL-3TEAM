# -*- coding: utf-8 -*-
"""표 칸 채우기 공용 도구 (base.Grid, fill.word_lines·order_words·grid_to_tabledata) — line_tables·rule_tables·clip_tables 가 쓴다.

예전엔 표 인식기 교체 틀(runner·line_grid·get_backend)도 여기 있었다. TATR·SLANet+ 는 2026-11-18, 교체 틀은 2026-11-19 뺐다
(포인트 문서 9개에서 구조 일치 기존 11 → TATR 4 · SLANet+ 6). 코드·모델은 exp1_baseline/_보관/표인식기_TATR_SLANet/ 에 둔다."""
from __future__ import annotations

from knowledge.ingestion.parsers.pdf.table_backends.base import Grid, TableRegion  # noqa: F401
