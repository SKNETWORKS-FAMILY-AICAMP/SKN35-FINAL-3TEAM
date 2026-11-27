# -*- coding: utf-8 -*-
"""원본 형식 읽기 (HWPX·HWP) → 블록 목록 → DoclingDocument.

실험 5(2026-11-16) 결정을 따른다: "HWP·HWPX 원본이 있으면 PDF 보다 원본" — 같은 내용 PDF 는 표 구조 최고 2/8, 원본은 8/8.
  HWPX : 우리 추출기 (hwpx.py, OWPML XML 직접 읽기 — 표 구조 8/8·병합 23/23, syhwp 는 4/8·0/23)
  HWP  : syhwp 0.0.8 (MIT, 순수 파이썬) — 설치돼 있을 때만. 없으면 이유를 남기고 실패로 돌려준다
  HWPML: 우리 추출기 (hwpml.py, 한글 XML 직접 읽기 — 법제처 법령 ".hwp" 가 실제로 이 형식)
원래 위치: exp1_baseline/1차실험/실험코드/parsekit (실험용) — 제품 코드로 옮겼다.

블록(block) = 형식에 상관없는 중간 표현.
  {"type": "para", "text": str, "style_level": int | None}
  {"type": "table", "rows": int, "cols": int, "cells": [(r, c, rs, cs, text)]}
  {"type": "furniture", "kind": "header" | "footer" | "footnote", "text": str}
"""
