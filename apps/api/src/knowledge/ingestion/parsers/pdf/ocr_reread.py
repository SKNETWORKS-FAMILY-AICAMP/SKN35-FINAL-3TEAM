# -*- coding: utf-8 -*-
"""이미지 쪽 글자 다시 읽기 (Docling 결과 후처리).

왜 필요한가
  글자층이 없는 쪽(이미지 파일, 스캔 PDF 쪽)은 Docling OCR 한 번으로 글자를 읽는다. 제목·굵은 글씨·손글씨체·
  어두운 배경 글씨는 자주 잘못 읽거나 놓친다(dev 카드뉴스 글자 보존율 59~66%. 예: '셀프 연임은 막고' →
  '연임은 막고 셀피'). 해상도를 바꾸면 읽히는 글자가 달라지는데, 문서마다 좋은 배율이 달라 하나로 고를 수 없다.
  (전체 쪽 OCR 모드는 카드뉴스는 올리고 스캔 표 쪽은 떨어뜨렸다.)

방법
  1) 쪽을 여러 배율로 OCR한다 (같은 RapidOCR mobile 모델).
     이미지 파일은 원본 픽셀의 1·2·3배, PDF 쪽은 108·216·324dpi로 그린다.
  2) 신뢰도 MIN_SCORE 이상인 줄만 모은다. 배율끼리 같은 자리(IoU > SAME_PLACE)에서 읽은 줄은 신뢰도가 높은 쪽만 남긴다.
  3) Docling 글자 항목마다 그 안에 들어오는 다시 읽은 줄(줄 넓이의 절반 이상이 항목 안)을 모은다.
     모은 줄 글자 수가 원래 글자 수의 REPLACE_MIN 이상이면 항목 글자를 다시 읽은 줄로 바꾼다.
     (조금만 읽힌 항목은 원래 글자를 지킨다 — 줄 일부만 다시 읽혀 나머지가 사라지는 것을 막음)
  4) 어느 항목에도 안 들어가고 표 안도 아닌 줄 = Docling이 놓친 글자 → 바로 위 항목 뒤에 새 글자로 넣는다.
     단, 주변 항목(줄 상자를 NEAR pt 넓혀 겹치는 항목) 글자에 줄 글자의 ALREADY 이상이 이미 있으면 넣지 않는다
     (dev S1 합성 스캔 1쪽은 기울어져 있어 줄 상자가 항목 상자 밖으로 삐져나와, 같은 문장이 두 번 들어갔다).
     그림 영역 안의 아주 짧은 줄(PIC_NOISE 글자 이하)이나 글자 없이 숫자·기호뿐인 줄은 아이콘·삽화 잡음이라 버린다
     (dev 카드뉴스 그림 영역 OCR: '0000', '호', '위', '원').
  5) 표 안 줄은 다시 읽지 않는다 — 표는 Docling 표 구조를 그대로 둔다
     (dev 사진 3쪽: 표 글자가 두 번 들어가 정확도 84% → 60%).

하지 않는 일
  - 글자층이 있는 쪽(words가 있는 쪽)은 건드리지 않는다.
  - 표 칸 글자 오독은 고치지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    PictureItem,
    ProvenanceItem,
    TableItem,
    TextItem,
)
from docling_core.types.doc import ContentLayer

MIN_SCORE = 0.9          # 이 신뢰도 이상인 줄만 쓴다
SAME_PLACE = 0.3         # 배율끼리 같은 자리로 보는 IoU
CONTAINED = 0.6          # 한 줄 상자가 다른 줄 상자 안에 이 비율 이상 들어가면 같은 자리 (배율마다 줄을 다르게 끊음)
SMALLER = 0.8            # 넓이가 상대의 이 비율 미만이면 '더 짧은 조각'
INSIDE = 0.5             # 줄 넓이의 이 비율 이상이 항목·표·그림 안이면 '안에 있다'
REPLACE_MIN = 0.5        # 다시 읽은 글자 수 ≥ 원래 글자 수 × 이 값이면 바꾼다
PIC_NOISE = 3            # 그림 안 이 글자 수 이하 줄은 버린다
NEAR = 12.0              # 놓친 줄인지 볼 때 줄 상자를 이만큼 넓혀 주변 항목을 찾는다 (pt)
ALREADY = 0.7            # 줄 글자의 이 비율 이상이 주변 항목 글자에 이미 있으면 '이미 읽힘'
IMAGE_SCALES = (1.0, 2.0, 3.0)      # 이미지 파일: 원본 픽셀 배율
PDF_DPI = (108, 216, 324)           # 스캔 PDF 쪽: 그리는 해상도


@dataclass
class RereadLog:
    pages: list[dict] = field(default_factory=list)


def make_engine(models_dir: str | Path):
    """RapidOCR 엔진 (Docling과 같은 모델 파일, 오프라인)."""
    from rapidocr import LangRec, OCRVersion, RapidOCR

    m = Path(models_dir)
    return RapidOCR(params={
        "Det.model_path": str(m / "ch_PP-OCRv5_det_mobile.onnx"),
        "Cls.model_path": str(m / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
        "Rec.model_path": str(m / "korean_PP-OCRv5_rec_mobile.onnx"),
        "Det.ocr_version": OCRVersion.PPOCRV5, "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.lang_type": LangRec.KOREAN, "Global.log_level": "warning",
    })


def _iou(a, b):
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _area(a):
    return max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])


def _inside(a, b):
    """a 넓이 중 b 안에 든 비율."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return inter / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))


def read_lines(engine, images: list[tuple[object, float]]) -> list[tuple[tuple, str, float]]:
    """여러 배율 그림을 OCR해 신뢰도 높은 줄을 모은다.

    images: [(PIL 그림, 그림 픽셀 → 쪽 좌표(pt) 배율)]. 돌려주는 줄: (쪽 좌표 상자 l,t,r,b, 글자, 신뢰도).
    """
    import numpy as np

    found: list[tuple[tuple, str, float]] = []
    for img, px2pt in images:
        r = engine(np.asarray(img.convert("RGB")))
        boxes = r.boxes if r.boxes is not None else []
        for box, txt, sc in zip(boxes, r.txts or [], r.scores or []):
            sc = float(sc)
            if sc < MIN_SCORE or not str(txt).strip():
                continue
            xs = [float(q[0]) * px2pt for q in box]
            ys = [float(q[1]) * px2pt for q in box]
            bb = (min(xs), min(ys), max(xs), max(ys))
            # 같은 자리: 많이 겹치거나(IoU), 한쪽이 다른 쪽 안에 들어간 경우(배율마다 줄을 다르게 끊음 —
            # dev 카드뉴스 지배구조_2에서 같은 문장이 두 번 들어감). 같은 자리 줄이 여럿이면 모두 비교한다.
            #   - 새 줄이 기존 줄 안에 들어감 → 기존(더 긴) 줄을 지킨다
            #   - 기존 줄이 새 줄 안에 들어감 → 새(더 긴) 줄로 바꾼다 (짧은 조각이 신뢰도가 높아도 글자를 잃지 않게)
            #   - 그냥 많이 겹침(IoU) → 신뢰도 높은 쪽
            #   ('안에 들어감'은 넓이가 확실히 작을 때(SMALLER 미만)만. 크기가 비슷하면 그냥 겹침으로 본다)
            if any(_inside(bb, f[0]) > CONTAINED and _area(bb) < SMALLER * _area(f[0]) for f in found):
                continue
            inner = [i for i, f in enumerate(found) if _inside(f[0], bb) > CONTAINED and _area(f[0]) < SMALLER * _area(bb)]
            overlap = [i for i, f in enumerate(found) if i not in inner and _iou(f[0], bb) > SAME_PLACE]
            if overlap and not all(sc > found[i][2] for i in overlap):
                continue
            drop = set(inner) | set(overlap)
            found = [f for i, f in enumerate(found) if i not in drop]
            found.append((bb, str(txt), sc))
    return found


def _order(lines):
    """읽기 순서: 위→아래로 줄을 묶고(세로 가운데가 앞 줄 높이의 절반 안이면 같은 줄), 같은 줄은 왼→오른.

    (예전 방식: 줄마다 '위끝 ÷ 자기 줄 높이'로 정렬 → 글자 크기가 다른 줄끼리 순서가 뒤집혔다. dev 카드뉴스 가상자산_1)
    """
    rows: list[list] = []
    for f in sorted(lines, key=lambda f: (f[0][1] + f[0][3]) / 2):
        cy = (f[0][1] + f[0][3]) / 2
        if rows:
            last = rows[-1][-1]
            lcy, lh = (last[0][1] + last[0][3]) / 2, last[0][3] - last[0][1]
            if abs(cy - lcy) <= 0.5 * lh:
                rows[-1].append(f)
                continue
        rows.append([f])
    return [f for row in rows for f in sorted(row, key=lambda f: f[0][0])]


def _pic_noise(text: str) -> bool:
    """그림 안 잡음: 짧거나(PIC_NOISE 글자 이하), 한글·영문 글자가 하나도 없음('0000', '√')."""
    t = "".join(text.split())
    return len(t) <= PIC_NOISE or not any(ch.isalpha() for ch in t)


def _already_read(line_text: str, near_text: str) -> bool:
    """줄 글자(공백 뺌) 중 주변 글자에 이미 있는 비율이 ALREADY 이상인가 (글자 세기)."""
    have: dict[str, int] = {}
    for ch in "".join(near_text.split()):
        have[ch] = have.get(ch, 0) + 1
    want = "".join(line_text.split())
    hit = 0
    for ch in want:
        if have.get(ch, 0) > 0:
            have[ch] -= 1
            hit += 1
    return bool(want) and hit >= ALREADY * len(want)


def _box(doc, prov) -> tuple:
    b = prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)
    return (b.l, b.t, b.r, b.b)


def reread_page(doc: DoclingDocument, page_no: int, lines: list) -> dict:
    """한 쪽에 다시 읽은 줄을 반영한다 (문서를 직접 고친다). 내역 dict."""
    items, tables, pics = [], [], []
    for it, _ in doc.iterate_items(included_content_layers=set(ContentLayer)):
        if not getattr(it, "prov", None) or it.prov[0].page_no != page_no:
            continue
        if isinstance(it, TableItem):
            tables.append(_box(doc, it.prov[0]))
        elif isinstance(it, PictureItem):
            pics.append(_box(doc, it.prov[0]))
        elif isinstance(it, TextItem):
            items.append(it)
    lines = [f for f in lines if not any(_inside(f[0], tb) > INSIDE for tb in tables)]   # 표 안은 표에 맡김
    used: set[int] = set()
    replaced = 0
    for it in items:
        ib = _box(doc, it.prov[0])
        mine = [i for i, f in enumerate(lines) if i not in used and _inside(f[0], ib) > INSIDE]
        if not mine:
            continue
        new = " ".join(f[1] for f in _order([lines[i] for i in mine]))
        if len("".join(new.split())) >= REPLACE_MIN * len("".join(it.text.split())):
            if new != it.text:
                it.text = new
                it.orig = new
                replaced += 1
            used.update(mine)
    # Docling이 놓친 줄: 그림 안 짧은 잡음은 버리고, 바로 위 항목 뒤에 넣는다
    rest = [f for i, f in enumerate(lines) if i not in used
            and not (any(_inside(f[0], pb) > INSIDE for pb in pics) and _pic_noise(f[1]))]

    def near_text(bb):
        x0, y0, x1, y1 = bb[0] - NEAR, bb[1] - NEAR, bb[2] + NEAR, bb[3] + NEAR
        out = []
        for it in items:
            ib = _box(doc, it.prov[0])
            if ib[0] < x1 and x0 < ib[2] and ib[1] < y1 and y0 < ib[3]:
                out.append(it.text)
        return " ".join(out)

    rest = [f for f in rest if not _already_read(f[1], near_text(f[0]))]
    added = 0
    H = doc.pages[page_no].size.height
    for f in _order(rest):
        l, t, r, b = f[0]
        prov = ProvenanceItem(page_no=page_no, charspan=(0, len(f[1])),
                              bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT).to_bottom_left_origin(H))
        above = [it for it in items if _box(doc, it.prov[0])[3] <= t + 1]
        if above:
            sib = max(above, key=lambda it: (_box(doc, it.prov[0])[3], _box(doc, it.prov[0])[0]))
            new_item = doc.insert_text(sibling=sib, label=DocItemLabel.TEXT, text=f[1], prov=prov, after=True)
        elif items:
            new_item = doc.insert_text(sibling=items[0], label=DocItemLabel.TEXT, text=f[1], prov=prov, after=False)
        else:
            new_item = doc.add_text(label=DocItemLabel.TEXT, text=f[1], prov=prov)
        items.append(new_item)
        added += 1
    return {"page": page_no, "lines_read": len(lines), "replaced": replaced, "added": added}


def page_images_from_file(path_or_bytes, doc: DoclingDocument) -> dict[int, list]:
    """이미지 파일 입력: 1쪽 그림을 IMAGE_SCALES 배율로. {쪽: [(그림, px→pt)]}"""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes).convert("RGB")
    pg = doc.pages[1].size
    base = pg.width / img.width                     # 원본 1픽셀 = base pt
    out = []
    for s in IMAGE_SCALES:
        im = img if s == 1 else img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
        out.append((im, base / s))
    return {1: out}


def page_images_from_pdf(pdf_bytes: bytes, pages: set[int]) -> dict[int, list]:
    """스캔 PDF 쪽: PDF_DPI 해상도로 그린다. {쪽: [(그림, px→pt)]}"""
    import io

    import pypdfium2 as pdfium

    out: dict[int, list] = {}
    pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    try:
        for i in range(len(pdf)):
            if i + 1 not in pages:
                continue
            page = pdf[i]
            out[i + 1] = [(page.render(scale=dpi / 72.0).to_pil(), 72.0 / dpi) for dpi in PDF_DPI]
            page.close()
    finally:
        pdf.close()
    return out


def reread_document(doc: DoclingDocument, engine, images: dict[int, list]) -> RereadLog:
    """images에 있는 쪽(글자층 없는 쪽)마다 다시 읽기를 반영한다."""
    log = RereadLog()
    for page_no, imgs in sorted(images.items()):
        if page_no not in doc.pages:
            continue
        log.pages.append(reread_page(doc, page_no, read_lines(engine, imgs)))
    return log
