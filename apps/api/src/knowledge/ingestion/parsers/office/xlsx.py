# -*- coding: utf-8 -*-
"""XLSX 후처리: Docling 이 읽은 엑셀 결과(DoclingDocument)를 원본 셀 정보(openpyxl)로 보완한다.

왜 필요한가 (합성 샘플 진단 2026-11-27, 실험데이터/J_엑셀합성)
  Docling 은 병합 칸·한 시트 여러 표·숨긴 시트·날짜·% 는 잘 읽는다. 그런데
  1) 시트 이름이 마크다운·제목 경로 어디에도 없다 → 청크가 어느 시트에서 왔는지 모른다
  2) 제목·메모 같은 '글 한 칸'을 1x1 표로 만든다 → 제목으로 못 쓰이고 단위 설명이 표처럼 떠 있다
  3) 숫자 서식을 무시한다: 1,250,000 → 1250000, 35,000,000원 → 35000000, (1,200,000) → -1200000
  4) 결과값이 저장되지 않은 수식 칸은 빈칸이 되고 기록도 없다
하는 일 (순서대로)
  1) 시트마다 맨 앞에 시트 이름을 제목(1단계)으로 넣는다
  2) 1x1 표: 그 시트의 첫 '진짜 표'(2칸 이상) 앞에 있으면 제목(2단계), 아니면 문단으로 바꾼다
  3) 표 칸 숫자에 원본 서식을 적용해 엑셀 화면에 보이는 글자로 바꾼다 (해석 못 하는 서식은 그대로 둔다)
  4) 결과값 없는 수식 칸은 log 에 남기고, 그 칸이 비어 있으면 수식 문자열을 넣는다 (계산은 하지 않는다)
Docling 의 엑셀 표는 prov bbox 에 시트 안 위치(l=시작 열, t=시작 행, 0부터)를 담는다 → 원본 칸과 맞출 수 있다.
"""
from __future__ import annotations

import io
import re

from docling_core.types.doc import BoundingBox, DocItemLabel, GroupItem, ProvenanceItem, TableItem
from docling_core.types.doc.labels import GroupLabel


# ------------------------------------------------------------------ 숫자 서식 → 보이는 글자
_COLOR = re.compile(r"\[(?!\$)[^\]]*\]")          # [Red] [Color10] [>=100] 같은 색·조건 → 버린다
_CURRENCY = re.compile(r"\[\$([^\]-]*)(?:-[^\]]*)?\]")   # [$₩-412] → '₩'
_DATEISH = re.compile(r"[yYmMdDhHsS]")             # 따옴표 밖에 이 글자가 있으면 날짜·시각 서식


def _sections(fmt: str) -> list[str]:
    """';' 로 서식 구역을 나눈다 (따옴표 안의 ';' 는 무시). 구역: 양수 ; 음수 ; 0 ; 글자"""
    out, cur, q = [], "", False
    for ch in fmt:
        if ch == '"':
            q = not q
        if ch == ";" and not q:
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _tokens(sec: str):
    """구역 하나 → (앞 글자, 숫자 틀, 뒤 글자). 숫자 틀은 0 # ? , . 로만 된 첫 덩어리.
    날짜·지수·백분율·글자(@) 서식이면 None (Docling 값 그대로 둔다)."""
    sec = _CURRENCY.sub(lambda m: '"' + m.group(1) + '"', sec)
    sec = _COLOR.sub("", sec)
    pre, num, post = "", "", ""
    plain = ""                                     # 따옴표·역슬래시 글자를 뺀 서식 (종류 판정용)
    state = "pre"                                  # pre: 숫자 틀 앞 / num: 숫자 틀 안 / post: 숫자 틀 뒤
    i = 0
    while i < len(sec):
        ch = sec[i]
        if ch == '"':                              # "원" 같은 따옴표 글자
            j = sec.find('"', i + 1)
            j = len(sec) if j < 0 else j
            lit, i = sec[i + 1:j], j + 1
        elif ch == "\\" and i + 1 < len(sec):      # \원 같은 역슬래시 글자
            lit, i = sec[i + 1], i + 2
        elif ch in "_*" and i + 1 < len(sec):      # _) 는 자리 띄움, *x 는 채움 → 버린다
            i += 2
            continue
        elif ch in "0#?,." and state != "post":    # 숫자 틀은 첫 덩어리 하나만
            num += ch; plain += ch; state = "num"; i += 1
            continue
        else:
            lit, plain, i = ch, plain + ch, i + 1
        if state == "pre":
            pre += lit
        else:
            state = "post"
            post += lit
    if not num or "%" in plain or "@" in plain or "E" in plain.upper() or _DATEISH.search(plain):
        return None
    return pre, num, post


def display_number(value: float, fmt: str) -> str | None:
    """엑셀 숫자 서식을 적용한 글자. 모르는 서식이면 None."""
    if not fmt or fmt == "General":
        return None
    secs = _sections(fmt)
    neg_own = len(secs) >= 2 and value < 0         # 음수 구역이 따로 있으면 부호 없이 그 구역으로
    if value == 0 and len(secs) >= 3:
        sec = secs[2]
    elif neg_own:
        sec = secs[1]
    else:
        sec = secs[0]
    tk = _tokens(sec)
    if tk is None:
        return None
    pre, num, post = tk
    # 숫자 틀 끝의 ',' 는 천 단위 나누기 (#,##0, → 천 단위)
    scale = 0
    while num.endswith(",") and not num.endswith(".,"):
        num = num[:-1]; scale += 1
    ip, _, dp = num.partition(".")
    v = abs(value) if (neg_own or value < 0) else value
    v = v / (1000 ** scale)
    lo, hi = dp.count("0"), len(dp)                # 소수: 0 은 꼭, # · ? 는 있으면
    s = f"{v:,.{hi}f}" if "," in ip else f"{v:.{hi}f}"
    if hi > lo and "." in s:                       # # 자리의 끝 0 은 지운다
        whole, frac = s.split(".")
        frac = frac.rstrip("0")
        frac = frac + "0" * max(0, lo - len(frac))
        s = whole + ("." + frac if frac else "")
    sign = "-" if (value < 0 and not neg_own) else ""
    return f"{sign}{pre}{s}{post}"


def _plain_forms(value) -> set[str]:
    """Docling 이 서식 없이 숫자를 썼을 때 나올 수 있는 글자들 (이것과 같을 때만 바꾼다)."""
    out = {str(value)}
    if isinstance(value, float) and value.is_integer():
        out.add(str(int(value)))
    return out


# ------------------------------------------------------------------ 본 처리
def fix_xlsx(doc, raw: bytes) -> dict:
    """doc 을 고친다. 반환: log (시트 수, 넣은 제목, 1칸 표 처리, 바꾼 표시값, 결과값 없는 수식)."""
    from openpyxl import load_workbook

    log = {"sheets": 0, "sheet_headings": 0, "single_cell_to_heading": 0, "single_cell_to_text": 0,
           "display_values": 0, "formula_no_cache": [], "skipped": []}
    try:
        wb_v = load_workbook(io.BytesIO(raw), data_only=True)       # 저장된 결과값
        wb_f = load_workbook(io.BytesIO(raw), data_only=False)      # 수식 문자열
    except Exception as e:                                          # 원본을 못 열면 Docling 결과만 쓴다
        log["skipped"].append(f"openpyxl 열기 실패: {type(e).__name__}")
        return log

    sheets = [g for g in doc.groups if g.label == GroupLabel.SHEET]
    to_delete = []
    for g in sheets:
        if g.name not in wb_v.sheetnames:
            log["skipped"].append(f"시트 못 찾음: {g.name}")
            continue
        ws_v, ws_f = wb_v[g.name], wb_f[g.name]
        kids = [c.resolve(doc) for c in g.children]
        if not kids:
            continue
        log["sheets"] += 1
        layer = g.content_layer                    # 숨긴 시트는 INVISIBLE — 넣는 항목도 같은 층에 둔다

        # 4) 결과값 없는 수식 (시트 전체) — 표 칸 채우기에도 쓴다
        no_cache = {}
        for row in ws_f.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("=") and ws_v[c.coordinate].value is None:
                    no_cache[(c.row, c.column)] = c.value
                    log["formula_no_cache"].append(f"{g.name}!{c.coordinate}")

        seen_table = False
        for it in kids:
            if not isinstance(it, TableItem) or not it.prov:
                continue
            d = it.data
            box = it.prov[0].bbox
            r0, c0 = int(box.t), int(box.l)        # 표의 시트 안 시작 위치 (0부터)
            # 2) 1x1 표 → 제목 또는 문단
            if d.num_rows == 1 and d.num_cols == 1:
                text = d.table_cells[0].text.strip() if d.table_cells else ""
                if text:
                    prov = ProvenanceItem(page_no=it.prov[0].page_no, bbox=box, charspan=(0, len(text)))
                    if not seen_table:
                        doc.insert_heading(sibling=it, text=text, level=2, prov=prov, content_layer=layer, after=False)
                        log["single_cell_to_heading"] += 1
                    else:
                        doc.insert_text(sibling=it, label=DocItemLabel.TEXT, text=text, prov=prov, content_layer=layer, after=False)
                        log["single_cell_to_text"] += 1
                to_delete.append(it)
                continue
            seen_table = True
            # 3) 표시값 · 4) 빈 수식 칸
            for cell in d.table_cells:
                rr, cc = r0 + cell.start_row_offset_idx + 1, c0 + cell.start_col_offset_idx + 1   # openpyxl 은 1부터
                src = ws_v.cell(rr, cc)
                if (rr, cc) in no_cache and not cell.text.strip():
                    cell.text = no_cache[(rr, cc)]
                    continue
                v = src.value
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                if cell.text.strip() not in _plain_forms(v):   # Docling 이 이미 서식을 입힌 값(%·날짜)은 그대로
                    continue
                shown = display_number(v, src.number_format)
                if shown is not None and shown != cell.text:
                    cell.text = shown
                    log["display_values"] += 1

        # 1) 시트 이름 제목 — 시트의 맨 앞 항목 앞에 넣는다 (1x1 제목보다 위, 1단계)
        first = next((c.resolve(doc) for c in g.children), None)
        if first is not None:
            page = first.prov[0].page_no if getattr(first, "prov", None) else 1
            doc.insert_heading(sibling=first, text=g.name, level=1, content_layer=layer, after=False,
                               prov=ProvenanceItem(page_no=page, bbox=BoundingBox(l=0, t=0, r=0, b=0), charspan=(0, len(g.name))))
            log["sheet_headings"] += 1

    if to_delete:
        doc.delete_items(node_items=to_delete)
    log["formula_no_cache"] = log["formula_no_cache"][:50]          # 기록이 너무 길어지지 않게
    return log
