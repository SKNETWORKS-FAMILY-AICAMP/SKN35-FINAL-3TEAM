# -*- coding: utf-8 -*-
"""그림 속 글자를 본문으로 꺼낸다 (Docling 결과 후처리).

왜 필요한가
  글자층 PDF에서 Docling은 그림(흐름도·인포그래픽) 영역 안 글자층 글자를 이미 읽어 '그림의 자식 글자'로 붙여 둔다.
  그런데 Markdown 내보내기와 iterate_items 기본값(traverse_pictures=False)은 그림 자식을 건너뛴다
  → 글자는 있는데 본문·청크에서 사라진다.
  dev: 공동주택회계감사(흐름도 2개) 글자 보존율 88.47%, InDesign 모집요강 98.49%.

하는 일
  본문 그림마다 자식 글자(TextItem)를 읽기 순서대로 모아 한 덩어리 글자로 만들고, 그림 바로 뒤에 본문 글자로 넣는다.
  옮긴 자식 글자는 그림에서 지운다(두 번 나오지 않게). 그림 자체(위치·이미지)는 그대로 둔다.
  줄 잇기: 앞 줄과 세로로 가깝고(줄 높이 GAP_LINES배 이내) 가로가 겹치면 같은 덩어리(공백으로 이음), 아니면 줄을 바꾼다.

하지 않는 일
  - 글자층이 없는 그림(래스터 그림)의 자식 글자는 OCR 결과다. 작은 그림 OCR은 잘 깨져서
    (dev 신구조문 18쪽: '구분'→'쿠분', '항목별 평가등급'→'활목을 평가통급') 여기서는 옮기지 않는다.
    words(쪽별 글자층 낱말)를 주면 자식 글자의 절반 이상이 글자층에 있을 때만 옮긴다.
  - 머리말·꼬리말 층 그림(로고 등)은 건드리지 않는다.
  - 본문 표·글자 항목과 자리가 겹치는 자식 글자는 옮기지 않는다 — 이미 본문에 있는 글자다.
    (시험셋 v2 로그: 점검표 일부가 그림으로도 잡혀 같은 글자가 두 번 들어가 정확도가 1.00 → 0.63)
  - 쪽 위·아래 여백의 작은 그림(로고)도 건너뛴다. dev 결재문서 2개: 서울시 로고의 글자층이 깨진 글자
    ('|Y롱lY', '20의 트별시')라 본문에 넣으면 정확도가 1.0 → 0.93으로 떨어졌다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from docling_core.types.doc import (
    BoundingBox,
    ContentLayer,
    DocItemLabel,
    DoclingDocument,
    PictureItem,
    ProvenanceItem,
    TextItem,
)

GAP_LINES = 1.2      # 두 줄 사이 세로 틈 ≤ 줄 높이 × 이 값이면 같은 덩어리
LOGO_AREA = 0.005    # 쪽 넓이의 이 비율보다 작고
LOGO_MARGIN = 0.15   # 쪽 위·아래 이 비율 안에 있는 그림은 로고로 보고 건너뛴다
LAYER_SHARE = 0.5    # 자식 글자 중 이 비율 이상이 글자층 낱말과 같아야 '글자층 글자'로 본다
OVERLAP_SKIP = 0.5   # 자식 글자 줄 넓이의 이 비율 이상이 본문 표·글자 항목과 겹치면 이미 본문에 있는 글자로 보고 뺀다


@dataclass
class PictureTextLog:
    pictures: list[dict] = field(default_factory=list)


def _tl(doc: DoclingDocument, prov: ProvenanceItem) -> BoundingBox:
    return prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)


def _join_lines(doc: DoclingDocument, items: list[TextItem]) -> str:
    """자식 글자들을 덩어리로 묶어 잇는다 (Docling이 준 순서를 읽기 순서로 쓴다)."""
    out: list[str] = []
    prev = None
    for it in items:
        txt = it.text.strip()
        if not txt:
            continue
        b = _tl(doc, it.prov[0]) if it.prov else None
        same_block = False
        if prev is not None and b is not None and it.prov[0].page_no == prev[0]:
            pb = prev[1]
            h = max(1.0, min(pb.b - pb.t, b.b - b.t))
            gap = b.t - pb.b
            x_overlap = min(pb.r, b.r) - max(pb.l, b.l)
            same_block = -0.5 * h <= gap <= GAP_LINES * h and x_overlap > 0
        if same_block and out:
            out[-1] = out[-1] + " " + txt
        else:
            out.append(txt)
        prev = (it.prov[0].page_no, b) if b is not None else None
    return "\n".join(out)


def _is_logo(doc: DoclingDocument, prov: ProvenanceItem) -> bool:
    """쪽 위·아래 여백에 있는 작은 그림."""
    pg = doc.pages[prov.page_no].size
    b = _tl(doc, prov)
    area = (b.r - b.l) * (b.b - b.t) / max(1.0, pg.width * pg.height)
    cy = (b.t + b.b) / 2                                   # 가운데가 여백 안이면 (dev 로고는 아래끝이 경계에 걸침)
    in_margin = cy <= LOGO_MARGIN * pg.height or cy >= (1 - LOGO_MARGIN) * pg.height
    return area < LOGO_AREA and in_margin


def _layer_share(doc: DoclingDocument, prov: ProvenanceItem, texts: list[TextItem], words: list) -> float:
    """자식 글자 중 그림 영역 안 글자층 낱말과 겹치는 글자 비율 (글자 세기)."""
    b = _tl(doc, prov)
    inside = "".join(w[0] for w in words if b.l - 2 <= (w[1] + w[3]) / 2 <= b.r + 2 and b.t - 2 <= (w[2] + w[4]) / 2 <= b.b + 2)
    have: dict[str, int] = {}
    for ch in "".join(inside.split()):
        have[ch] = have.get(ch, 0) + 1
    kid = "".join("".join(t.text.split()) for t in texts)
    hit = 0
    for ch in kid:
        if have.get(ch, 0) > 0:
            have[ch] -= 1
            hit += 1
    return hit / max(1, len(kid))


def _body_boxes(doc: DoclingDocument) -> dict[int, list[tuple]]:
    """쪽마다 본문 표·글자 항목 상자 (그림의 자식은 빼고)."""
    from docling_core.types.doc import TableItem

    out: dict[int, list[tuple]] = {}
    for it, _ in doc.iterate_items():                       # 기본값: 그림 안은 안 들어감
        if isinstance(it, (TableItem, TextItem)) and getattr(it, "prov", None):
            for p in it.prov:
                b = _tl(doc, p)
                out.setdefault(p.page_no, []).append((b.l, b.t, b.r, b.b))
    return out


def _covered(b, boxes) -> bool:
    area = max(1e-6, (b.r - b.l) * (b.b - b.t))
    for l, t, r, bb in boxes:
        inter = max(0.0, min(b.r, r) - max(b.l, l)) * max(0.0, min(b.b, bb) - max(b.t, t))
        if inter / area >= OVERLAP_SKIP:
            return True
    return False


def lift_picture_text(doc: DoclingDocument, words: dict[int, list] | None = None) -> PictureTextLog:
    """본문 그림의 자식 글자를 그림 뒤 본문 글자로 옮긴다 (문서를 직접 고친다).

    words: 쪽 번호 → 글자층 낱말 [(글자, l, t, r, b)] (pdf_lines.read_pdf_words). 주면 OCR로 생긴 자식 글자는 건너뛴다.
    """
    log = PictureTextLog()
    pics = [p for p, _ in doc.iterate_items() if isinstance(p, PictureItem)]   # 기본값: 본문 층만
    boxes = _body_boxes(doc)
    for pic in pics:
        if pic.prov and _is_logo(doc, pic.prov[0]):
            continue
        kids = [c.resolve(doc) for c in pic.children]
        texts = [k for k in kids if isinstance(k, TextItem) and k.text.strip()]
        dup = [k for k in texts if k.prov and _covered(_tl(doc, k.prov[0]), boxes.get(k.prov[0].page_no, []))]
        texts = [k for k in texts if k not in dup]          # 이미 본문에 있는 자리의 글자는 빼고 (그림에는 남겨 둔다)
        if not texts:
            continue
        if words is not None and pic.prov:
            share = _layer_share(doc, pic.prov[0], texts, words.get(pic.prov[0].page_no, []))
            if share < LAYER_SHARE:
                continue                                   # 글자층에 없는 글자 = OCR 결과 → 옮기지 않음
        joined = _join_lines(doc, texts)
        # 위치: 그림 위치를 그대로 쓴다 (글자가 그림 안에 있으므로)
        prov = None
        if pic.prov:
            p0 = pic.prov[0]
            prov = ProvenanceItem(page_no=p0.page_no, bbox=p0.bbox, charspan=(0, len(joined)))
        doc.insert_text(sibling=pic, label=DocItemLabel.TEXT, text=joined, prov=prov,
                        content_layer=ContentLayer.BODY, after=True)
        doc.delete_items(node_items=texts)
        log.pictures.append({"page": pic.prov[0].page_no if pic.prov else None, "lines": len(texts),
                             "chars": len("".join(joined.split()))})
    return log
