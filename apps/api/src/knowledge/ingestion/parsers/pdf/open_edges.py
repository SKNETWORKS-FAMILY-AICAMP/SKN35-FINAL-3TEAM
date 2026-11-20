# -*- coding: utf-8 -*-
"""양옆이 트인 표 닫기: 좌·우 바깥 세로 테두리를 생략한 표에 가상 테두리를 합성한다.

이식 출처와 고지
  kordoc (https://github.com/chrisryugj/kordoc) — MIT License, Copyright 2026 chrisryugj.
  pdf/line-extract.ts closeOpenTableEdges · chainCollinearRules 를 Python 으로 옮겼다.
  변경 사항: 선 형식을 우리 PageLines((y, x0, x1) / (x, y0, y1), 왼쪽 위 원점)로 바꿨다. 선 굵기 정보가 없어
  '굵은 바깥 테두리 · 가는 안쪽 선' 으로 쌓인 표를 가르는 판정(EDGE_FRAME_WIDTH_K)은 옮기지 않았다(간격 판정만).

왜 필요한가
  한국 행정문서 표는 좌·우 바깥 테두리를 긋지 않는 모양이 흔하다(가로 괘선은 전폭, 세로선은 안쪽 구분선만).
  우리 선 격자(line_tables)는 이런 표의 바깥 열을 잃어 일부러 건너뛰고(WIDTH_COVER·한쪽 트임) TableFormer 결과를 썼다.
  시험셋 v2 서울 공연예술제 13x5·19x5, 요양원 11x5 가 이 모양이고 kordoc 은 가상 테두리로 다 맞혔다.

방법 (원본과 같다)
  1) 같은 y 에 끊어 그은 가로선 조각을 논리 괘선 하나로 잇는다(체인).
  2) 양끝 x 가 EDGE_ALIGN_TOL 안으로 같은 괘선끼리 묶는다. 같은 폭 표가 위아래로 쌓였으면 큰 세로 간격에서 가른다
     (간격이 중앙값의 EDGE_YGAP_SPLIT_K 배 넘고, 그 띠를 건너지르는 세로선이 없을 때만).
  3) 괘선 EDGE_MIN_RULES 개 이상·높이 EDGE_MIN_SPAN 이상 묶음에, 안쪽(양끝에서 EDGE_INSET 안) 세로선이 괘선 둘 이상과
     실제로 교차하면 표다. 그 묶음의 왼끝·오른끝 근처에 세로선이 없으면 그 자리에 가상 세로 테두리를 합성한다.
"""
from __future__ import annotations

EDGE_ALIGN_TOL = 3.0      # 끝점 정렬 묶음 허용 오차 (pt)
EDGE_MIN_RULES = 3        # 묶음 최소 괘선 수
EDGE_MIN_SPAN = 12.0      # 묶음 최소 높이 (pt)
EDGE_INSET = 15.0         # 안쪽 세로선: 묶음 좌우 끝에서 이만큼 안
EDGE_NEAR = 10.0          # 변 근처에 이미 세로선이 있다고 보는 거리
EDGE_CONNECT_TOL = 5.0    # 교차·닿음 판정 여유
EDGE_YGAP_SPLIT_K = 2.5   # 쌓인 표 가르기 — 간격 중앙값의 이 배
EDGE_YGAP_ABS_MIN = 30.0  # 〃 최소 간격 (pt)
CHAIN_Y_TOL = 1.5         # 체인: 같은 괘선으로 보는 y 오차
CHAIN_GAP = 3.0           # 체인: 잇는 최대 틈


def chain_collinear(hs):
    """같은 y 의 이어진 가로선 조각을 논리 괘선 하나로 (판정 전용 — 원래 선은 바꾸지 않는다)."""
    if len(hs) <= 1:
        return list(hs)
    srt = sorted(hs, key=lambda h: (h[0], h[1]))
    rules, start = [], 0

    def flush(end):
        band = sorted(srt[start:end], key=lambda h: h[1])
        y, x0, x1 = band[0]
        for hy, a, b in band[1:]:
            if a - x1 <= CHAIN_GAP:
                x1 = max(x1, b)
            else:
                rules.append((y, x0, x1))
                y, x0, x1 = hy, a, b
        rules.append((y, x0, x1))
    for i in range(1, len(srt) + 1):
        if i == len(srt) or srt[i][0] - srt[start][0] > CHAIN_Y_TOL:
            flush(i)
            start = i
    return rules


def close_open_edges(hs, vs):
    """가로선 hs [(y, x0, x1)], 세로선 vs [(x, y0, y1)] → 합성한 가상 세로 테두리 목록 [(x, y0, y1)]."""
    if len(hs) < EDGE_MIN_RULES:
        return []
    # 1) 끝점 정렬 묶음
    groups: list[list] = []
    for hl in chain_collinear(hs):
        for g in groups:
            if abs(g[0][1] - hl[1]) <= EDGE_ALIGN_TOL and abs(g[0][2] - hl[2]) <= EDGE_ALIGN_TOL:
                g.append(hl)
                break
        else:
            groups.append([hl])
    # 1b) 같은 폭으로 위아래 쌓인 표를 큰 간격에서 가른다 (사이 본문을 관통하는 가상 테두리 방지)
    split, cuts = [], []
    for g in groups:
        srt = sorted(g, key=lambda h: h[0])
        gaps = sorted(srt[i][0] - srt[i - 1][0] for i in range(1, len(srt)))
        median = gaps[len(gaps) >> 1] if gaps else 0.0
        cur = [srt[0]] if srt else []
        for i in range(1, len(srt)):
            lo, hi = srt[i - 1][0], srt[i][0]
            gx1, gx2 = min(srt[i - 1][1], srt[i][1]), max(srt[i - 1][2], srt[i][2])
            # 그 띠를 건너지르는 세로선(병합 큰 행의 좌우변·안쪽 구분선)이 있으면 표 안 큰 행이다
            bridged = any(v[1] <= lo + EDGE_NEAR and v[2] >= hi - EDGE_NEAR and gx1 - EDGE_CONNECT_TOL <= v[0] <= gx2 + EDGE_CONNECT_TOL
                          for v in vs)
            if median > 0 and hi - lo > median * EDGE_YGAP_SPLIT_K and hi - lo > EDGE_YGAP_ABS_MIN and not bridged \
                    and len(cur) >= EDGE_MIN_RULES and len(srt) - i >= EDGE_MIN_RULES:
                split.append(cur)
                cur = []
                cuts.append((lo, hi, gx1, gx2))
            cur.append(srt[i])
        if cur:
            split.append(cur)
    # 한 묶음에서 가른 띠는 겹쳐 놓인 다른 묶음도 가른다
    if cuts:
        parts = []
        for g in split:
            gx1, gx2 = min(h[1] for h in g), max(h[2] for h in g)
            own = [c for c in cuts if min(c[3], gx2) - max(c[2], gx1) >= (gx2 - gx1) * 0.5]
            cur = []
            for h in g:
                if cur and any(cur[-1][0] <= c[0] + EDGE_CONNECT_TOL and h[0] >= c[1] - EDGE_CONNECT_TOL for c in own):
                    parts.append(cur)
                    cur = []
                cur.append(h)
            if cur:
                parts.append(cur)
        split = parts
    # 2)·3) 안쪽 세로선이 실존하는 묶음의 열린 변에 가상 테두리
    out = []
    for g in split:
        if len(g) < EDGE_MIN_RULES:
            continue
        y_min, y_max = min(h[0] for h in g), max(h[0] for h in g)
        x1 = sum(h[1] for h in g) / len(g)
        x2 = sum(h[2] for h in g) / len(g)
        if y_max - y_min < EDGE_MIN_SPAN:
            continue

        def cross(v):
            return sum(1 for h in g if h[1] - EDGE_CONNECT_TOL <= v[0] <= h[2] + EDGE_CONNECT_TOL
                       and v[1] - EDGE_CONNECT_TOL <= h[0] <= v[2] + EDGE_CONNECT_TOL)
        if not any(x1 + EDGE_INSET < v[0] < x2 - EDGE_INSET and cross(v) >= 2 for v in vs):
            continue
        for ex in (x1, x2):
            closed = any(abs(v[0] - ex) <= EDGE_NEAR and v[1] <= y_max + EDGE_CONNECT_TOL and v[2] >= y_min - EDGE_CONNECT_TOL for v in vs)
            if not closed:
                out.append((ex, y_min, y_max))
    return out
