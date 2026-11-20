# -*- coding: utf-8 -*-
"""혼합 방식: 표마다 우리 구조와 kordoc 구조 중 하나를 고른다 (글자·제목·읽기 순서는 늘 우리 것).

왜
  외부 오픈소스 파서 kordoc(MIT)은 공문서 표 구조(병합 칸)를 우리보다 잘 잡지만, 일부 문서에서 글자를 크게 놓친다
  (계획_결과/kordoc_비교결과.md). 우리는 그 반대다. 표마다 나은 쪽을 고르면 칸 단위 F1 상한이 v2 0.858 → 0.964.
  1단계 진단(혼합방식_계획.md 7절): TableFormer(+표 보정)가 만든 표는 kordoc이 18승 7패, 선 격자 표는 대체로 비김.

규칙 (2단계, 단순하게)
  1) 짝짓기: 같은 쪽, 위치 IoU ≥ PAIR_IOU. kordoc 표에 위치가 없으면 같은 쪽에서 칸 글자 Jaccard ≥ PAIR_TXT.
     kordoc 위치는 PDF 원점(왼쪽 아래), y = 표 아래 끝 → 위쪽 기준 t = H − (y + height).
  2) 우리 표가 선 격자(line_tables)나 규칙선 표(rule_tables)로 만든 것이면(protected) 우리 것을 쓴다.
     (kordoc 행이 더 많을 때만 kordoc 을 보는 변형은 v2·v3 칸 F1 이 나빠져 2026-11-19 뺐다.)
  3) 그 밖(TableFormer + 표 보정)이면 kordoc 구조를 쓴다. 단 안전장치:
     글자 보존 — 우리 표 자리의 글자층 글자 중 kordoc 칸 글자에 있는 비율이 KEEP_CHARS 미만이면 우리 것
     (kordoc이 글자를 놓친 표: 사옥 8x4, 원화대출 4x10 — 맞힘 0).
     반대로 kordoc 칸 글자 중 그 자리 글자층에 없는 것이 EXTRA_MAX를 넘어도 우리 것 (글자 정확도 보호).
  4) kordoc에만 있는 표는 이번 단계에서 넣지 않는다 (보도자료 머리 상자 같은 장식이 많고, 글자 중복 정리가 필요).

kordoc 표 JSON: {"rows", "cols", "cells": [[{"text", "rowSpan", "colSpan"}, ...], ...]} — 전체 격자, 병합 칸은 기준 칸에만
폭이 있고 가려진 자리는 빈 칸 → 가려진 자리는 건너뛴다.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from docling_core.types.doc import DoclingDocument, TableCell, TableData, TableItem, TextItem

PAIR_IOU = 0.5
PAIR_TXT = 0.5
KEEP_CHARS = 0.9
EXTRA_MAX = 0.1     # kordoc 칸 글자 중 우리 표 자리 글자층에 없는 글자가 이 비율을 넘으면 우리 것 (글자 정확도 보호:
                    # 쪽을 잘라낸 식약처 PDF에서 kordoc 표가 그 자리 밖 글자를 담아 정확도 0.997 → 0.78, 2단계 진단)
NEIGHBOR_PAGES = 3 # 여러 쪽 kordoc 표가 흡수할 수 있는 우리 표의 쪽 거리
ABSORB = 0.8        # 이웃 우리 표 글자의 이 비율 이상이 kordoc 표에 있으면 같은 표의 다른 쪽 조각으로 보고 함께 바꾼다
TEXT_ABSORB = 0.9   # 여러 쪽 클립 표의 뒤 쪽 글 문단: 글자의 이 비율 이상이 후보 표에 있으면 표 조각 (표보다 엄격 — 문단은 짧아 우연히 들기 쉽다)
TEXT_MIN_CHARS = 4  # 이보다 짧은 문단(쪽 번호·"현 행" 같은 머리 낱말)은 흡수 증거로 쓰지 않는다
TEXT_X_SLACK = 6.0  # 문단 가운데가 표 좌우 범위(첫 쪽 표 상자) 밖으로 이만큼까지는 안


@dataclass
class KordocLog:
    replaced: list[dict] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)
    items: list = field(default_factory=list, repr=False)   # 바꾸거나 넣은 표 객체 (참조 번호는 뒤 삭제로 바뀌므로 객체로 잡는다)


def kordoc_table_data(tb: dict) -> TableData | None:
    grid = tb.get("cells") or []
    covered, cells = set(), []
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if (r, c) in covered:
                continue
            rs, cs = int(cell.get("rowSpan", 1) or 1), int(cell.get("colSpan", 1) or 1)
            covered |= {(rr, cc) for rr in range(r, r + rs) for cc in range(c, c + cs)} - {(r, c)}
            cells.append(TableCell(start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                                   end_col_offset_idx=c + cs, row_span=rs, col_span=cs, text=cell.get("text") or ""))
    if not cells:
        return None
    nr = max(len(grid), max(c.end_row_offset_idx for c in cells))
    nc = max(max((len(r) for r in grid), default=0), max(c.end_col_offset_idx for c in cells))
    return TableData(num_rows=nr, num_cols=nc, table_cells=cells)


def _chars(s: str) -> Counter:
    return Counter("".join(s.split()))


def _iou(a, b):
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    i = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def load_kordoc_tables(path: str | Path, doc: DoclingDocument) -> list[dict]:
    """kordoc JSON → [{page, box(왼쪽 위 기준) | None, data(TableData)}]."""
    k = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for b in k.get("blocks") or []:
        if b.get("type") != "table" or not b.get("table"):
            continue
        data = kordoc_table_data(b["table"])
        pg = b.get("pageNumber")
        if data is None or pg not in doc.pages:
            continue
        box = None
        if b.get("bbox"):
            H = doc.pages[pg].size.height
            x, y, w, h = (float(b["bbox"][key]) for key in ("x", "y", "width", "height"))
            box = (x, H - y - h, x + w, H - y)
        out.append({"page": pg, "box": box, "data": data})
    return out


def apply_kordoc_tables(doc: DoclingDocument, kordoc_tables: list[dict], words: dict[int, list] | None,
                        protected: set[str], always_keep: set[str] | None = None, trusted: bool = False) -> KordocLog:
    """우리 표마다 짝 kordoc 표가 있고, 보호 대상(선 격자·규칙선 표)이 아니고, 글자 보존을 넘으면 구조를 kordoc 것으로 바꾼다.

    trusted: 후보가 원본 칸 경계 그대로인 표(한컴 PDF 칸 클립 — clip_tables)이면 보호 대상·규칙선 표라도 후보를 쓴다.
      선 격자·규칙선 표는 선을 보고 칸을 추정한 것이고, 칸 클립은 HWPX 칸 그 자체다(병합·보이지 않는 칸 쪼개기 포함). kordoc 도 칸 클립
      격자를 선 격자보다 앞세운다. v2 진단: 식약처 동등성 21x8(규칙선 표 10x3이 막음)·25x11(선 격자 25x9)·의료기기 5x2(선 격자 4x2)를
      칸 클립은 정답 그대로 냈는데 보호 때문에 못 바꿨다. 글자 보존 안전장치(KEEP_CHARS·EXTRA_MAX)는 그대로 거친다.

    always_keep: 어떤 설정에서도 우리 것을 쓸 표(규칙선 표 — 글머리 나누기 결과가 kordoc보다 정답에 가깝다:
    '행 더 많으면 kordoc' 실험에서 공연예술제 598→510, 정신의료기관 125→77로 kordoc이 더 나빴다)."""
    log = KordocLog()
    used = set()
    tables = [t for t, _ in doc.iterate_items() if isinstance(t, TableItem) and t.prov]
    absorbed: set[str] = set()                                  # kordoc 여러 쪽 표에 흡수돼 지울 우리 표
    absorbed_text: list = []                                    # 여러 쪽 클립 표에 흡수돼 지울 우리 글 문단

    def region_chars(tt):
        """우리 표 자리의 글자층 글자 (글자층이 없으면 우리 칸 글자)."""
        pp = tt.prov[0]
        b = pp.bbox.to_top_left_origin(doc.pages[pp.page_no].size.height)
        ws = (words or {}).get(pp.page_no) or []
        inside = [w for w in ws if b.l <= (w[1] + w[3]) / 2 <= b.r and b.t <= (w[2] + w[4]) / 2 <= b.b]
        return _chars(" ".join(w[0] for w in inside)) if inside else _chars(" ".join(c.text for c in tt.data.table_cells))

    for t in tables:
        if t.self_ref in absorbed:
            continue
        p = t.prov[0]
        bb = p.bbox.to_top_left_origin(doc.pages[p.page_no].size.height)
        box = (bb.l, bb.t, bb.r, bb.b)
        mine = _chars(" ".join(c.text for c in t.data.table_cells))
        best, score, how = None, 0.0, ""
        for j, k in enumerate(kordoc_tables):
            if j in used or k["page"] != p.page_no:
                continue
            if k["box"] is not None:
                s, h = _iou(box, k["box"]), "위치"
                if s < PAIR_IOU:
                    continue
            else:
                theirs = _chars(" ".join(c.text for c in k["data"].table_cells))
                s, h = sum((mine & theirs).values()) / max(1, sum((mine | theirs).values())), "글자"
                if s < PAIR_TXT:
                    continue
            if s > score:
                best, score, how = j, s, h
        info = {"page": p.page_no, "ref": t.self_ref, "ours": f"{t.data.num_rows}x{t.data.num_cols}"}
        if best is None:
            log.kept.append({**info, "why": "kordoc 짝 없음"})
            continue
        used.add(best)
        k = kordoc_tables[best]
        info["kordoc"] = f"{k['data'].num_rows}x{k['data'].num_cols}"
        if t.self_ref in (always_keep or set()) and not trusted:
            log.kept.append({**info, "why": "규칙선 표(늘 우리 것)"})
            continue
        if t.self_ref in protected and not trusted:
            # 선 격자·규칙선 표는 우리 것. ('kordoc 행이 더 많으면 kordoc' 변형은 칸 F1 v3 0.852 → 0.830,
            # v2 0.886 → 0.873 으로 나빠져 뺐다, 2026-11-19)
            log.kept.append({**info, "why": "선 격자·규칙선 표"})
            continue
        before = region_chars(t)
        theirs = _chars(" ".join(c.text for c in k["data"].table_cells))
        share = sum((before & theirs).values()) / max(1, sum(before.values()))
        if share < KEEP_CHARS:
            log.kept.append({**info, "why": f"kordoc 글자 보존 부족({share:.2f})"})
            continue
        extra = 1 - sum((before & theirs).values()) / max(1, sum(theirs.values()))
        group, text_group = [], []
        if extra > EXTRA_MAX:
            # kordoc이 여러 쪽에 걸친 표를 이미 하나로 합쳐 낸 경우: 남는 글자가 이웃 쪽 우리 표들에 있으면 그 표들까지 함께 바꾼다
            # (2단계 진단: 식약처 잘라낸 PDF 42x7·36x6 — 한 쪽 표만 바꾸면 이웃 쪽 표 글자가 두 번 나와 정확도 0.997 → 0.78).
            pool = before.copy()
            # 흡수할 수 있는 쪽: 후보 표가 실제로 걸친 뒤 쪽들뿐이다. 쪽 목록을 아는 후보(클립 표)는 그 목록, 모르는 후보(kordoc 원본)는
            # 뒤로 NEIGHBOR_PAGES 쪽까지. 앞 쪽은 이 표의 조각일 수 없다. 예전엔 앞뒤 ±3쪽을 글자 겹침만으로 흡수해, 법령 개정안의
            # 신구조문 표가 앞 쪽의 다른 표(과태료 33x5)까지 삼켜 글자가 사라졌다 (최종 비교 v4: 전자증권법 개정안 글자 재현율 0.985 → 0.949)
            span = (k.get("pages") or [])
            absorb_pages = set(span[1:]) if span else set(range(p.page_no + 1, p.page_no + NEIGHBOR_PAGES + 1))
            for o in tables:
                # 보호 표는 trusted 여도 흡수하지 않는다 — trusted 는 짝지어진 그 표만 바꾼다. 다른 쪽 보호 표까지 흡수하면 원본에서 따로인
                # 표를 하나로 잇거나(dev 시금고 검사 38x4 + 다음 쪽 → 59x4) 글자가 덜 든 이은 표로 15쪽을 덮었다(dev 신구조문 대비표 글자 1.0 → 0.92)
                if o is t or o.self_ref in absorbed or o.self_ref in protected or not o.prov:
                    continue
                if o.prov[0].page_no not in absorb_pages:
                    continue
                oc = region_chars(o)
                if sum((oc & theirs).values()) >= ABSORB * max(1, sum(oc.values())):
                    group.append(o)
                    pool += oc
            # 여러 쪽에 걸친 클립 표(pages 를 아는 후보 — Python 이식판)는 뒤 쪽 조각을 우리 Docling 이 표로 못 찾고 글 문단으로
            # 둔 경우가 있다(금융위 신구조문 대비표: 이은 표는 정답과 같은 11x2인데 뒤 쪽이 문단이라 흡수할 표가 없어 '자리 밖 글자'로 거절).
            # 그 쪽들에서 표 열 범위 안에 놓이고 글자가 거의 다 후보 표에 든 문단도 같은 표의 조각으로 보고 함께 바꾼다(문단은 지운다).
            more_pages = set((k.get("pages") or [])[1:])
            if more_pages and extra > EXTRA_MAX:
                for it, _ in doc.iterate_items():
                    if not isinstance(it, TextItem) or not it.prov or it.prov[0].page_no not in more_pages:
                        continue
                    ip = it.prov[0]
                    ib = ip.bbox.to_top_left_origin(doc.pages[ip.page_no].size.height)
                    cx = (ib.l + ib.r) / 2
                    if not (box[0] - TEXT_X_SLACK <= cx <= box[2] + TEXT_X_SLACK):
                        continue                                    # 표 열 범위 밖 (쪽 옆 글·다른 단)
                    ic = _chars(it.text)
                    if sum(ic.values()) < TEXT_MIN_CHARS or sum((ic & theirs).values()) < TEXT_ABSORB * sum(ic.values()):
                        continue
                    text_group.append(it)
                    pool += ic
            extra = 1 - sum((pool & theirs).values()) / max(1, sum(theirs.values()))
            # 흡수한 표·문단까지 합친 글자도 후보에 KEEP_CHARS 이상 남아야 한다 (첫 쪽 글자만 보면 뒤 쪽 글자가 빠진 후보를 놓친다)
            kept_all = sum((pool & theirs).values()) / max(1, sum(pool.values()))
            if (group or text_group) and kept_all < KEEP_CHARS:
                log.kept.append({**info, "why": f"흡수한 쪽 글자 보존 부족({kept_all:.2f})"})
                continue
            if extra > EXTRA_MAX:
                log.kept.append({**info, "why": f"kordoc 표에 그 자리 밖 글자({extra:.2f})"})
                continue
        t.data = k["data"]
        for o in group:
            absorbed.add(o.self_ref)
        absorbed_text.extend(text_group)
        rec = {**info, "pair": how, "kept_chars": round(share, 2), "extra": round(extra, 2)}
        if group:
            rec["absorbed_pages"] = [o.prov[0].page_no for o in group]
        if text_group:
            rec["absorbed_text"] = len(text_group)
        log.replaced.append(rec)
        log.items.append(t)
    gone = [o for o in tables if o.self_ref in absorbed] + absorbed_text
    if gone:
        doc.delete_items(node_items=gone)
    return log
