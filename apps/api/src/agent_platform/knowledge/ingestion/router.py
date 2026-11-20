"""router: 파일 하나를 보고 "무슨 형식인지"와 "기준 B의 어느 그룹인지"를 정한다.

기준 B(구조를 얻는 방법)에 따라 다음 단계가 갈린다.
  written  (적혀 있음) : DOCX·XLSX·PPTX·HTML·메일·CSV·MD → Docling 구조 경로
                          HWPX·HWPML                     → 우리 추출기
  infer    (추측)      : 디지털 PDF                       → Docling 모델 경로 (OCR 기본 모드: 그림 영역만)
  ocr      (글자부터)  : 스캔·깨진 PDF, 이미지             → Docling 모델 경로 + OCR (PDF는 ocr_plan대로)
  convert  (변환)      : 바이너리 HWP                     → 변환 후 다시 router
  reject   (거부)      : 옛 Office, 암호·DRM, 알 수 없음  → 사유를 남기고 멈춤

원칙
  - 확장자를 믿지 않는다. 파일 앞 바이트(겉 포장) → 안쪽 표식(속) 순서로 본다.
    (법제처 ".hwp"가 실제로는 HWPML이었다.)
  - 파일 내용을 꺼내지 않는다. 판별만 한다. 그래서 빠르고, 실패해도 다음 단계에 영향이 없다.
  - 절대 예외를 밖으로 던지지 않는다. 실패도 reject + 사유로 돌려준다(운영·감사 로그용).
  - 판별 근거(evidence)를 항상 남긴다. 규제 산업에서 "왜 이 경로로 갔나"를 설명해야 한다.

사용: python -m agent_platform.knowledge.ingestion.router <파일 또는 폴더> ...
의존성: docling==2.150.1 (docling-parse·olefile 포함). Docling 내부 함수를 쓰므로 버전 고정.
"""

from __future__ import annotations

import re
import sys
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

HEAD_BYTES = 4096          # 앞부분만 읽는다. PDF 서명은 처음 1KB 안 어디든 올 수 있다(규격).
PDF_SIG_WINDOW = 1024


class Group(str, Enum):
    WRITTEN = "written"     # 적혀 있음
    INFER = "infer"         # 추측
    OCR = "ocr"             # 글자부터
    CONVERT = "convert"     # 변환
    REJECT = "reject"       # 거부


class Handler(str, Enum):
    DOCLING = "docling"
    HWPX_EXTRACT = "hwpx_extract"
    HWPML_EXTRACT = "hwpml_extract"
    HWP_CONVERT = "hwp_convert"
    REJECT = "reject"


@dataclass
class OcrPlan:
    """다음 단계(Docling 변환)에 넘길 OCR 설정. 라우터는 정하기만 하고 적용은 핸들러가 한다.

    do_ocr          : 항상 True. 기본 모드(PDF_AWARE_LAYOUT_REGIONS)는 그림이 있거나
                      보이는 글자가 없는 영역만 OCR하므로, 멀쩡한 디지털 PDF는 글자가 그대로다
                      (실측: 법령 PDF에서 OCR 켬/끔 결과 동일). 끄면 글자+그림 섞인 쪽의 그림 글자를 잃는다.
    full_page_pages : 글자층이 깨진 쪽(broken + qmark). 핸들러가 이 쪽만 page_range로 따로
                      FULL_PAGE OCR 변환한다(FULL_PAGE는 변환 전체에 걸리는 설정이라 쪽을 나눠야 함).

    Docling의 replace_broken_text_layer는 쓰지 않는다. 문서 전체에 Docling 자체 판정을 거는데,
    우리 샘플에서 오탐이 36쪽 나왔다(별표1의 기호 글꼴 U+F000, 한국은행 영문 보고서 각주의 U+0007).
    라우터가 오탐을 걸러 낸 쪽 목록을 대신 넘긴다.
    """
    do_ocr: bool = True
    full_page_pages: list[int] = field(default_factory=list)


@dataclass
class PdfTextLayer:
    """PDF 쪽마다 글자층 상태. 쪽 번호는 1부터.
    Docling 기본 PDF 백엔드(docling-parse)로 읽은 결과다 — 변환 때 Docling이 보는 글자와 같아야 하기 때문."""
    pages: int = 0
    empty_pages: list[int] = field(default_factory=list)      # 글자 0개 → 스캔/그림 쪽
    invisible_pages: list[int] = field(default_factory=list)  # 보이지 않는 글자만 있음 → 스캐너가 OCR해 둔 스캔본
    broken_pages: list[int] = field(default_factory=list)     # Docling 판정으로 깨짐 (제어문자·대체문자 등)
    qmark_pages: list[int] = field(default_factory=list)      # 단어 가운데 '?' (Docling 판정 밖)
    image_pages: list[int] = field(default_factory=list)      # 글자도 있고 그림도 큼 (기록용, 기본 OCR이 처리)

    @property
    def needs_ocr(self) -> bool:
        """글자층만으로는 안 되는 쪽이 있나 (그룹 OCR 판정용)."""
        return bool(self.empty_pages or self.invisible_pages or self.broken_pages or self.qmark_pages)

    @property
    def ocr_plan(self) -> OcrPlan:
        return OcrPlan(full_page_pages=sorted(self.broken_pages + self.qmark_pages))


@dataclass
class RouteResult:
    path: Path
    format: str                      # pdf, hwpx, hwp, hwpml, docx, xlsx, pptx, html, eml, msg, image, csv, md, unknown …
    group: Group
    handler: Handler
    evidence: list[str] = field(default_factory=list)
    pdf: PdfTextLayer | None = None
    reject_reason: str = ""
    # Docling 경로일 때 핸들러에 넘길 것 ----------------------------------------------
    # docling_ext: Docling은 형식을 다시 확장자·내용으로 추측한다. 확장자 없는 메일("mail")은 건너뛰고(skipped),
    #   .txt로 받은 HTML은 운 좋게 맞췄다(실측). 핸들러는 DocumentStream(name=이름+docling_ext)로 넘겨 형식을 고정한다.
    docling_ext: str = ""
    # encoding: 텍스트 형식의 실제 인코딩. Docling CSV·MD 백엔드는 모르면 UTF-8 → cp1252로 추측하는데,
    #   cp949 CSV를 cp1252로 읽어 한글이 "»óÇ°"로 깨진 채 success를 냈다(실측). 핸들러는 encoding 옵션으로 넘긴다.
    encoding: str = ""

    def summary(self) -> str:
        extra = ""
        if self.pdf:
            p = self.pdf
            extra = (f" pages={p.pages} empty={len(p.empty_pages)} invisible={len(p.invisible_pages)}"
                     f" broken={len(p.broken_pages)} qmark={len(p.qmark_pages)}")
        why = f" reason={self.reject_reason}" if self.reject_reason else ""
        return f"{self.format:<6} {self.group.value:<8} {self.handler.value:<14}{extra}{why} | {'; '.join(self.evidence)}"


# ---------------------------------------------------------------------------
# 1단계: 겉 포장 (앞 바이트)
# ---------------------------------------------------------------------------
OLE_SIG = bytes.fromhex("D0CF11E0A1B11AE1")
IMAGE_SIGS = [                                   # (서명, 이름)
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
]


def sniff(head: bytes) -> str:
    """앞 바이트로 겉 포장 종류만 정한다: pdf | zip | ole | image:<종류> | text | binary | empty."""
    if not head:
        return "empty"
    if b"%PDF-" in head[:PDF_SIG_WINDOW]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(OLE_SIG):
        return "ole"
    for sig, name in IMAGE_SIGS:
        if head.startswith(sig):
            return f"image:{name}"
    return "text" if _looks_like_text(head) else "binary"


def _looks_like_text(head: bytes) -> bool:
    """UTF-8 또는 CP949로 읽히고 제어문자가 거의 없으면 텍스트로 본다(한글 CSV는 CP949가 흔함)."""
    sample = head[:2048]
    if b"\x00" in sample:                       # NUL이 있으면 텍스트가 아님 (UTF-16은 지금 범위 밖)
        return False
    text = _decode_prefix(sample)
    if text is None:
        return False
    ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t\f")
    return ctrl <= len(text) * 0.01


def _decode_prefix(data: bytes) -> str | None:
    """파일 앞부분만 잘라 읽으면 마지막 한글 글자가 중간에서 잘린다.
    증분 디코더(final=False)는 잘린 꼬리를 오류로 보지 않고 다음 입력을 기다리므로, 앞부분 판별에 맞다.
    UTF-8로 안 되면 CP949(한글 CSV에 흔함)로 본다. 둘 다 안 되면 None."""
    import codecs
    for enc in ("utf-8-sig", "cp949"):
        try:
            return codecs.getincrementaldecoder(enc)().decode(data, final=False)
        except UnicodeDecodeError:
            continue
    return None


def _decode_text(head: bytes) -> str:
    text = _decode_prefix(head)
    return text if text is not None else head.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 2단계: 속 표식
# ---------------------------------------------------------------------------
OOXML_PREFIX = {"word/": "docx", "xl/": "xlsx", "ppt/": "pptx"}


def inspect_zip(path: Path) -> tuple[str, list[str]]:
    """zip 안을 본다. HWPX는 mimetype 파일, OOXML은 [Content_Types].xml + 폴더 이름으로 가른다."""
    ev = ["zip"]
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "mimetype" in names:
                mt = z.read("mimetype")[:100].decode("ascii", errors="replace").strip()
                ev.append(f"mimetype={mt}")
                if mt == "application/hwp+zip":
                    return "hwpx", ev
                return "zip_other", ev           # ODF, EPUB 등 → 범위 밖
            if "[Content_Types].xml" in names:
                for prefix, fmt in OOXML_PREFIX.items():
                    if any(n.startswith(prefix) for n in names):
                        ev.append(f"ooxml {prefix}")
                        return fmt, ev
            ev.append(f"entries={names[:3]}")
            return "zip_other", ev
    except zipfile.BadZipFile as e:
        ev.append(f"bad zip: {e}")
        return "zip_broken", ev


def classify_ole(streams: list[str], fileheader: bytes) -> tuple[str, list[str]]:
    """OLE 안의 스트림 이름으로 가른다. 파일을 열지 않는 순수 함수라 테스트하기 쉽다.

    streams    : '/'로 이은 스트림 경로 목록 (예: 'BinData/BIN0001.jpg')
    fileheader : HWP의 'FileHeader' 스트림 앞부분 (없으면 b'')
    """
    tops = {s.split("/")[0] for s in streams}
    # 암호 걸린 DOCX·XLSX·PPTX는 zip이 아니라 OLE 안에 EncryptedPackage로 들어 있다 → 옛 Office로 오판하지 않게 먼저 본다
    if "EncryptedPackage" in tops:
        return "ooxml_encrypted", ["ole", "EncryptedPackage"]
    if fileheader.startswith(b"HWP Document File"):
        ev = ["ole", "FileHeader=HWP Document File"]
        # HWP 5.0 FileHeader: 서명 32바이트 + 버전 4바이트 + 속성 4바이트(little endian)
        # 속성 비트(한글 문서 파일 형식 5.0 공개 문서):
        #   bit1 암호 · bit2 배포용 · bit4 DRM 보안 · bit8 공인인증서 암호화 · bit10 공인인증서 DRM
        # bit1은 실제 암호 샘플(props=0x3), bit2는 실제 배포용 샘플(props=0x5, ViewText 있음)로 확인. 나머지는 공개 문서 기준.
        if len(fileheader) >= 40:
            props = int.from_bytes(fileheader[36:40], "little")
            ev.append(f"props=0x{props:08x}")
            if props & (1 << 1 | 1 << 8):
                return "hwp_password", ev
            if props & (1 << 4 | 1 << 10):
                return "hwp_drm", ev
            if props & (1 << 2):
                return "hwp_distribution", ev
        # 두 번째 표식: 배포용 문서는 본문을 BodyText가 아니라 암호화된 ViewText 스트림에 둔다.
        # 속성 비트가 어긋나도 이 스트림이 있으면 본문을 읽을 수 없으므로 배포용으로 본다.
        if "ViewText" in tops:
            ev.append("ViewText stream")
            return "hwp_distribution", ev
        return "hwp", ev
    if any(t.startswith("__substg1.0_") or t == "__properties_version1.0" for t in tops):
        return "msg", ["ole", "outlook __substg1.0_"]
    legacy = {"WordDocument": "doc", "Workbook": "xls", "Book": "xls", "PowerPoint Document": "ppt"}
    for name, fmt in legacy.items():
        if name in tops:
            return f"legacy_{fmt}", ["ole", name]
    return "ole_other", ["ole", f"streams={sorted(tops)[:4]}"]


def inspect_ole(path: Path) -> tuple[str, list[str]]:
    import olefile                                   # Docling 설치 시 함께 들어옴 (python-oxmsg 의존성)
    try:
        with olefile.OleFileIO(str(path)) as ole:
            streams = ["/".join(s) for s in ole.listdir()]
            fh = ole.openstream("FileHeader").read(64) if ole.exists("FileHeader") else b""
        return classify_ole(streams, fh)
    except OSError as e:
        return "ole_broken", ["ole", f"open failed: {e}"]


EML_HEADER = re.compile(r"^(From|To|Subject|Received|Date|MIME-Version|Message-ID|Return-Path|Content-Type):", re.I)


def classify_text(head: bytes, suffix: str) -> tuple[str, list[str]]:
    """텍스트 계열. 서명이 없으니 내용 패턴 → 확장자 순으로 본다."""
    text = _decode_text(head[:HEAD_BYTES]).lstrip("﻿ \r\n\t")
    low = text[:1024].lower()
    if low.startswith("<?xml") or low.startswith("<!doctype") or low.startswith("<"):
        if re.search(r"<hwpml[\s>]", text[:2048], re.I):
            return "hwpml", ["xml", "root=HWPML"]
        if "<!doctype html" in low or re.search(r"<html[\s>]", low):
            return "html", ["html tag"]
        return "xml_other", ["xml, root not HWPML/HTML"]
    lines = text.splitlines()[:30]
    hits = sum(1 for ln in lines if EML_HEADER.match(ln))
    if hits >= 2:                                    # 메일 머리글 줄이 2개 이상이면 EML
        return "eml", [f"rfc822 headers={hits}"]
    by_ext = {".csv": "csv", ".tsv": "csv", ".md": "md", ".markdown": "md"}
    if suffix in by_ext:
        return by_ext[suffix], ["text", f"ext={suffix}"]
    return "text_other", ["text", f"ext={suffix or '-'}"]


# ---------------------------------------------------------------------------
# 3단계: PDF 글자층 (추측 vs 글자부터)
# ---------------------------------------------------------------------------
# 판별 엔진 = 변환 엔진. 예전엔 pypdfium2로 봤는데, font_pb.pdf는 pypdfium2에선 악센트가 '?'로 나오고
# docling-parse에선 멀쩡했다(실측 '?' 0개). 다른 엔진으로 보면 멀쩡한 파일을 OCR로 보내 글자를 잃는다.
#
# 깨짐 신호 1: Docling 자체 판정(docling.utils.text_quality.is_broken_text_layer)을 쓴다.
#   입력도 Docling 전처리 단계와 같다(보이는 줄 글자를 줄바꿈으로 이음). 단, 오탐 하나를 먼저 걸러 낸다:
#   사용자 영역(PUA) 문자나 제어문자가 쪽 전체에 1~2종류뿐이면 기호 글꼴·구분자라 보고 지운다.
#     오탐 실측: 별표1 → U+F000 한 종류(기호 글꼴), 한국은행 영문 보고서 → U+0007 한 종류(각주 번호 뒤 구분자).
#     진짜 깨짐: anon-test → U+FFFD(대응 없음 표시)가 75%. U+FFFD는 지우지 않으므로 그대로 잡힌다.
#   글꼴 대응이 통째로 PUA·제어문자로 밀린 경우는 글자마다 코드가 달라 종류가 많으므로 남는다.
# 깨짐 신호 2: 글꼴에 대응표가 없으면 악센트·한글이 '?'로 바뀌어 단어 가운데에 '?'가 낀다(예: "co?ts").
#   Docling 판정은 이걸 잡지 않는다. 샘플 PDF 200여 쪽(pypdfium2 기준)에서 이런 쪽은 깨진 파일 1쪽뿐
#   (글자의 2.75%), 나머지는 0% → 1%를 기준으로 둔다.
INWORD_QMARK_RATIO = 0.01
_INWORD_QMARK = re.compile(r"(?<=\w)\?(?=\w)")
IMAGE_AREA_NOTE = 0.05   # 그림이 쪽 넓이의 5% 이상이면 image_pages에 기록 (판정이 아니라 근거용)
FEW_KINDS = 2            # PUA·제어문자가 이 종류 수 이하면 기호·구분자로 본다


def _drop_symbol_like(text: str) -> str:
    """종류가 적은 PUA 문자·제어문자(공백류 제외)를 지운다. U+FFFD는 건드리지 않는다."""
    import unicodedata
    def kind(ch: str) -> str | None:
        cat = unicodedata.category(ch)
        if cat == "Co":
            return "pua"
        if cat == "Cc" and not ch.isspace():
            return "ctrl"
        return None
    seen: dict[str, set[str]] = {"pua": set(), "ctrl": set()}
    for ch in text:
        k = kind(ch)
        if k:
            seen[k].add(ch)
    drop = set().union(*(s for s in seen.values() if len(s) <= FEW_KINDS))
    return "".join(ch for ch in text if ch not in drop) if drop else text

_parser = None


def _docling_parser():
    """docling-parse 파서는 만들 때 비용이 있으니 한 번만 만든다."""
    global _parser
    if _parser is None:
        from docling_parse.pdf_parser import DoclingPdfParser
        _parser = DoclingPdfParser()
    return _parser


def _page_content_config():
    """판별에 필요한 것만 꺼낸다: 줄 글자(+계산용 문자), 그림 위치. 그림 바이트·도형은 안 꺼낸다."""
    from docling_parse.pdf_parser import ContentConfig, ContentLevel
    return ContentConfig(
        char_cells_content_level=ContentLevel.COMPUTE,           # 줄을 만들려면 계산은 해야 함
        word_cells_content_level=ContentLevel.SKIP,
        line_cells_content_level=ContentLevel.COMPUTE_AND_MATERIALIZE,
        shapes_content_level=ContentLevel.SKIP,
        bitmaps_content_level=ContentLevel.COMPUTE_AND_MATERIALIZE,
        include_bitmap_bytes=False,
    )


def classify_pdf_page(visible: list[str], invisible_count: int, image_share: float) -> str:
    """쪽 하나의 상태. 파일 없이 테스트할 수 있게 글자 목록만 받는 순수 함수.

    visible         : 보이는 줄 글자 목록 (docling-parse textline, 렌더링 모드가 보임)
    invisible_count : 보이지 않는 줄 개수 (스캐너 OCR 글자층 등)
    image_share     : 그림이 덮는 넓이 / 쪽 넓이
    반환: empty | invisible | broken | qmark | image | ok
    """
    from docling.utils.text_quality import is_broken_text_layer
    joined = "\n".join(visible)
    chars = "".join(joined.split())
    if not chars:
        return "invisible" if invisible_count else "empty"
    if is_broken_text_layer(_drop_symbol_like(joined)):
        return "broken"
    if len(_INWORD_QMARK.findall(chars)) / max(1, sum(c.isalpha() for c in chars)) >= INWORD_QMARK_RATIO:
        return "qmark"
    return "image" if image_share >= IMAGE_AREA_NOTE else "ok"


def inspect_pdf_text(path: Path) -> tuple[PdfTextLayer | None, list[str]]:
    """쪽마다 글자층 상태를 본다. 내용을 쓰지는 않고 판정만 한다."""
    from docling.backend.docling_parse_backend import _INVISIBLE_RENDERING_MODES
    from docling_parse.pdf_parser import PdfLoadError
    try:
        doc = _docling_parser().load(path, content_config=_page_content_config())
    except PdfLoadError as e:                        # 암호 PDF(PdfPasswordError 포함), 깨진 PDF
        return None, [f"pdf open failed: {type(e).__name__}"]
    layer = PdfTextLayer(pages=doc.number_of_pages())
    bucket = {"empty": layer.empty_pages, "invisible": layer.invisible_pages, "broken": layer.broken_pages,
              "qmark": layer.qmark_pages, "image": layer.image_pages}
    try:
        for no, page in doc.iterate_pages():
            cells = page.textline_cells
            visible = [c.text for c in cells if c.rendering_mode not in _INVISIBLE_RENDERING_MODES]
            area = page.dimension.width * page.dimension.height
            image = sum(b.rect.to_bounding_box().area() for b in page.bitmap_resources) / area if area else 0.0
            state = classify_pdf_page(visible, len(cells) - len(visible), image)
            if state in bucket:
                bucket[state].append(no)
    finally:
        doc.unload()                                 # 파서 안 캐시·파일 핸들 정리
    bad = len(layer.empty_pages) + len(layer.invisible_pages) + len(layer.broken_pages) + len(layer.qmark_pages)
    ev = [f"text layer(docling-parse): {layer.pages - bad}/{layer.pages} pages ok"]
    for name in ("invisible", "broken", "qmark", "image"):
        if bucket[name]:
            ev.append(f"{name}={bucket[name]}")
    return layer, ev


# ---------------------------------------------------------------------------
# 종합
# ---------------------------------------------------------------------------
WRITTEN_DOCLING = {"docx", "xlsx", "pptx", "html", "eml", "msg", "csv", "md"}
# Docling이 형식을 알아보는 확장자 (형식 고정용)
DOCLING_EXT = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "html": ".html",
               "eml": ".eml", "msg": ".msg", "csv": ".csv", "md": ".md"}
IMAGE_EXT = {"png": ".png", "jpeg": ".jpg", "tiff": ".tif", "gif": ".gif", "bmp": ".bmp"}
# Docling이 인코딩을 cp1252로 추측해 버리는 형식 → 인코딩을 못 정하면 거부(조용히 깨진 글자보다 낫다)
NEEDS_ENCODING = {"csv", "md"}


def detect_encoding(path: Path) -> str | None:
    """텍스트 파일 전체를 UTF-8 → CP949 순서로 엄격하게 디코딩해 본다. 둘 다 안 되면 None.
    앞부분만 보면 앞은 ASCII(영문 머리글)이고 뒤에 한글이 있는 cp949 파일을 UTF-8로 오판하므로 전체를 본다.
    CP949는 EUC-KR을 포함한다. BOM이 있으면 utf-8-sig로 알려 준다(Docling이 BOM을 존중하지만 명시가 안전)."""
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ("utf-8", "cp949"):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return None


REJECT_REASONS = {
    "text_encoding": "인코딩을 정할 수 없는 텍스트 (UTF-8·CP949 아님)",
    "legacy_doc": "옛 Word(.doc): 이번 범위 제외",
    "legacy_xls": "옛 Excel(.xls): 이번 범위 제외",
    "legacy_ppt": "옛 PowerPoint(.ppt): 이번 범위 제외",
    "ooxml_encrypted": "암호 걸린 Office 문서",
    "hwp_password": "암호 걸린 HWP",
    "hwp_distribution": "배포용 HWP (편집·변환 제한)",
    "hwp_drm": "DRM 보안 HWP",
    "zip_other": "지원하지 않는 zip 계열 (ODF·EPUB 등)",
    "zip_broken": "깨진 zip",
    "ole_other": "알 수 없는 OLE 문서",
    "ole_broken": "깨진 OLE 문서",
    "xml_other": "HWPML·HTML이 아닌 XML",
    "text_other": "형식을 정할 수 없는 텍스트",
    "binary": "알 수 없는 바이너리 (암호화·DRM 의심)",
    "empty": "빈 파일",
    "pdf_locked": "열 수 없는 PDF (암호 또는 손상)",
}


def route(path: str | Path) -> RouteResult:
    path = Path(path)
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError as e:
        return RouteResult(path, "unknown", Group.REJECT, Handler.REJECT, [f"read failed: {e}"], reject_reason="파일을 읽을 수 없음")

    kind = sniff(head)
    ev = [f"sniff={kind}"]
    pdf_layer = None

    if kind == "pdf":
        pdf_layer, more = inspect_pdf_text(path)
        ev += more
        fmt = "pdf" if pdf_layer else "pdf_locked"
    elif kind == "zip":
        fmt, more = inspect_zip(path)
        ev += more
    elif kind == "ole":
        fmt, more = inspect_ole(path)
        ev += more
    elif kind.startswith("image:"):
        fmt = "image"
        ev.append(kind.split(":")[1])
    elif kind == "text":
        fmt, more = classify_text(head, path.suffix.lower())
        ev += more
    else:                                            # binary, empty
        fmt = kind

    # 텍스트 형식은 인코딩을 정한다 (Docling에 넘길 값)
    encoding = ""
    if kind == "text" and fmt in WRITTEN_DOCLING:
        enc = detect_encoding(path)
        ev.append(f"encoding={enc or '?'}")
        if enc:
            encoding = enc
        elif fmt in NEEDS_ENCODING:
            fmt = "text_encoding"

    # 형식 → 기준 B 그룹 → 다음 단계
    if fmt == "pdf":
        group = Group.OCR if pdf_layer.needs_ocr else Group.INFER
        return RouteResult(path, fmt, group, Handler.DOCLING, ev, pdf=pdf_layer, docling_ext=".pdf")
    if fmt == "image":
        return RouteResult(path, fmt, Group.OCR, Handler.DOCLING, ev, docling_ext=IMAGE_EXT[kind.split(":")[1]])
    if fmt == "hwpx":
        return RouteResult(path, fmt, Group.WRITTEN, Handler.HWPX_EXTRACT, ev)
    if fmt == "hwpml":
        return RouteResult(path, fmt, Group.WRITTEN, Handler.HWPML_EXTRACT, ev)
    if fmt in WRITTEN_DOCLING:
        return RouteResult(path, fmt, Group.WRITTEN, Handler.DOCLING, ev,
                           docling_ext=DOCLING_EXT[fmt], encoding=encoding)
    if fmt == "hwp":
        return RouteResult(path, fmt, Group.CONVERT, Handler.HWP_CONVERT, ev)
    if path.suffix.lower() and fmt != "empty":
        ev.append(f"ext={path.suffix.lower()} (참고만)")
    return RouteResult(path, fmt, Group.REJECT, Handler.REJECT, ev,
                       reject_reason=REJECT_REASONS.get(fmt, "알 수 없음"))


def _iter_files(args: list[str]):
    for a in args:
        p = Path(a)
        if p.is_dir():
            yield from sorted(x for x in p.rglob("*") if x.is_file() and x.name != "README.md")
        else:
            yield p


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")     # Windows 콘솔에서 한글 경로 출력
    for p in _iter_files(sys.argv[1:] or ["samples"]):
        r = route(p)
        print(f"{p.as_posix():<70} {r.summary()}")
