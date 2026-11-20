"""router 테스트.

1) 실제 샘플(samples/)의 기대값: 정답이 사람이 정한 표 그대로다(README·라우터 설계 5번 표).
2) 합성 파일(tmp_path): 샘플이 없는 형식(DOCX·PPTX·HTML·메일·이미지·CSV)과 함정(확장자 거짓말, DRM 흉내, 빈 파일).
3) classify_ole 순수 함수: OLE 파일을 직접 못 만드는 경우(암호 Office, 옛 Office, MSG, 암호·배포용 HWP).

실행 (apps/api에서): uv run pytest -q tests/unit
샘플 위치: 환경변수 INGEST_SAMPLES_DIR, 없으면 <저장소>/data/fixtures/ingestion. 샘플이 없으면 해당 테스트만 건너뜀.
"""

import os
import shutil
import zipfile
from pathlib import Path

import pytest

from knowledge.ingestion.router import Group, Handler, classify_ole, classify_pdf_page, route, sniff

REPO = Path(__file__).resolve().parents[3]
SAMPLES = Path(os.environ.get("INGEST_SAMPLES_DIR", REPO / "data" / "fixtures" / "ingestion"))
needs_samples = pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ 없음")


# ---------------------------------------------------------------- 1) 실제 샘플
SAMPLE_EXPECT = [
    # (샘플 경로, format, group, handler)
    ("fsc_regulation_240426/여전감독규정_개정예고.pdf", "pdf", Group.INFER, Handler.DOCLING),
    ("fsc_regulation_240426/여전감독규정_개정예고.hwpx", "hwpx", Group.WRITTEN, Handler.HWPX_EXTRACT),
    ("fsc_regulation_240426/여전감독규정_개정예고.hwp", "hwp", Group.CONVERT, Handler.HWP_CONVERT),
    ("fsc_press_261113/보도자료_주택시장.hwpx", "hwpx", Group.WRITTEN, Handler.HWPX_EXTRACT),
    ("law_go_kr/대부업등감독규정_2024-24호_HWPML.hml", "hwpml", Group.WRITTEN, Handler.HWPML_EXTRACT),
    ("law_go_kr/대부업등감독규정_본문_2026-47호.pdf", "pdf", Group.INFER, Handler.DOCLING),
    ("bok_fsr_202606/그림데이터.xlsx", "xlsx", Group.WRITTEN, Handler.DOCLING),
    ("scan_synthetic/대부업등감독규정_본문_2026-47호_scan.pdf", "pdf", Group.OCR, Handler.DOCLING),
    ("scan_synthetic/hana_여신거래기본약관_신구대비표_2016_scan.pdf", "pdf", Group.OCR, Handler.DOCLING),
    # 라이브러리로 만든 실제 형식 파일 (eval/make_samples.py)
    ("generated/심사지침_가상.docx", "docx", Group.WRITTEN, Handler.DOCLING),
    ("generated/심사현황_가상.pptx", "pptx", Group.WRITTEN, Handler.DOCLING),
    ("generated/금리_한도표_가상.xlsx", "xlsx", Group.WRITTEN, Handler.DOCLING),
    ("generated/금리표_가상_cp949.csv", "csv", Group.WRITTEN, Handler.DOCLING),
    ("generated/금리표_가상_utf8.csv", "csv", Group.WRITTEN, Handler.DOCLING),
    ("generated/여신공지_가상.html", "html", Group.WRITTEN, Handler.DOCLING),
    ("generated/심사요청_가상.eml", "eml", Group.WRITTEN, Handler.DOCLING),
    ("generated/감독규정_1쪽_스캔.png", "image", Group.OCR, Handler.DOCLING),
    ("generated/감독규정_1쪽_스캔.jpg", "image", Group.OCR, Handler.DOCLING),
    ("generated/감독규정_1쪽_스캔.tif", "image", Group.OCR, Handler.DOCLING),
    # 웹에서 받은 실제 Outlook 파일
    ("web_msg/TestMessage-default.msg", "msg", Group.WRITTEN, Handler.DOCLING),
]


# 사용자가 암호·배포용을 직접 걸어 둔 파일 (samples/to_protect/README.md). 아직 안 걸었으면 건너뜀.
PROTECT_EXPECT = [
    ("암호_심사지침.docx", "ooxml_encrypted"),
    ("암호_금리한도표.xlsx", "ooxml_encrypted"),
    ("암호_개정예고.hwp", "hwp_password"),
    ("배포용문서.hwp", "hwp_distribution"),      # 팀원이 만든 실제 배포용 파일 (props=0x5, ViewText 있음)
    ("암호_개정예고-보호됨.pdf", "pdf_locked"),
]


# 웹에서 받은 글자층이 깨진 PDF
@needs_samples
def test_broken_text_layer_glyph_ids():
    # 글리프 번호가 제어문자·대체문자로 나옴 (MuPDF 포럼) → Docling 판정에 걸림 → 그 쪽만 OCR로 교체
    p = SAMPLES / "web_broken_pdf" / "anon-test.pdf"
    if not p.exists():
        pytest.skip("파일 없음")
    r = route(p)
    assert r.group == Group.OCR and r.pdf.broken_pages == [1], r.summary()
    assert r.pdf.ocr_plan.full_page_pages == [1]


@needs_samples
def test_symbol_font_is_not_broken():
    # 별표1의 U+F000(기호 글꼴 한 종류) 때문에 Docling 자체 판정은 10·12·13·20쪽을 깨짐으로 본다 → 오탐
    r = route(SAMPLES / "law_go_kr/별표1_재무건전성비율산출기준_저축은행시행세칙.pdf")
    assert r.group == Group.INFER and not r.pdf.broken_pages, r.summary()


@needs_samples
def test_font_pb_is_fine_in_docling_parse():
    # pypdfium2에선 악센트가 '?'로 나오지만(Ghostscript 버그 703823), Docling이 쓰는 docling-parse는 제대로 읽는다.
    # 판별 엔진을 변환 엔진과 맞췄으니 OCR로 보내면 안 된다(전체 OCR은 글자를 잃음).
    p = SAMPLES / "web_broken_pdf" / "font_pb.pdf"
    if not p.exists():
        pytest.skip("파일 없음")
    r = route(p)
    assert r.group == Group.INFER and not r.pdf.needs_ocr, r.summary()


@needs_samples
@pytest.mark.parametrize("name,fmt", PROTECT_EXPECT)
def test_protected_files(name, fmt):
    p = SAMPLES / "to_protect" / name
    if not p.exists():
        pytest.skip("파일 없음")
    r = route(p)
    if r.group != Group.REJECT and fmt != "pdf_locked":
        pytest.skip("아직 암호·배포용 설정 전 (원본 그대로)")
    if fmt == "pdf_locked" and r.format == "pdf":
        pytest.skip("아직 PDF 암호 설정 전")
    assert r.format == fmt and r.group == Group.REJECT, r.summary()


@needs_samples
@pytest.mark.parametrize("rel,fmt,group,handler", SAMPLE_EXPECT)
def test_samples(rel, fmt, group, handler):
    r = route(SAMPLES / rel)
    assert (r.format, r.group, r.handler) == (fmt, group, handler), r.summary()
    assert r.evidence, "판별 근거가 비어 있으면 안 됨"


@needs_samples
def test_scan_pdf_reports_empty_pages():
    r = route(SAMPLES / "scan_synthetic/대부업등감독규정_본문_2026-47호_scan.pdf")
    assert r.pdf.pages == 2 and r.pdf.empty_pages == [1, 2]


@needs_samples
def test_extension_lie_hwpml_named_hwp(tmp_path):
    # 법제처처럼 HWPML을 .hwp 이름으로 받은 경우 → 내용으로 판별해야 함
    fake = tmp_path / "규정.hwp"
    shutil.copy(SAMPLES / "law_go_kr/대부업등감독규정_2024-24호_HWPML.hml", fake)
    assert route(fake).format == "hwpml"


@needs_samples
def test_extension_lie_pdf_named_docx(tmp_path):
    fake = tmp_path / "보고서.docx"
    shutil.copy(SAMPLES / "fsc_regulation_240426/여전감독규정_개정예고.pdf", fake)
    assert route(fake).format == "pdf"


# ---------------------------------------------------------------- 2) 합성 파일
def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return path


def test_docx(tmp_path):
    p = _zip(tmp_path / "a.bin", {"[Content_Types].xml": "<Types/>", "word/document.xml": "<w:document/>"})
    r = route(p)
    assert (r.format, r.group, r.handler) == ("docx", Group.WRITTEN, Handler.DOCLING)


def test_pptx(tmp_path):
    p = _zip(tmp_path / "a.pptx", {"[Content_Types].xml": "<Types/>", "ppt/presentation.xml": "<p/>"})
    assert route(p).format == "pptx"


def test_odt_rejected(tmp_path):
    p = _zip(tmp_path / "a.odt", {"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": "<x/>"})
    r = route(p)
    assert r.group == Group.REJECT and "zip" in r.reject_reason


def test_html(tmp_path):
    p = tmp_path / "notice.txt"
    p.write_text("<!DOCTYPE html><html><body>공지</body></html>", encoding="utf-8")
    assert route(p).format == "html"


def test_eml(tmp_path):
    p = tmp_path / "mail"
    p.write_text("From: a@example.com\nTo: b@example.com\nSubject: 심사 요청\n\n본문", encoding="utf-8")
    r = route(p)
    assert (r.format, r.group) == ("eml", Group.WRITTEN)


def test_csv_cp949(tmp_path):
    p = tmp_path / "금리표.csv"
    p.write_bytes("상품,금리\n신용대출,5.1\n".encode("cp949"))
    assert route(p).format == "csv"


def test_png_goes_to_ocr(tmp_path):
    p = tmp_path / "scan.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    r = route(p)
    assert (r.format, r.group) == ("image", Group.OCR)


def test_unknown_binary_is_drm_suspect(tmp_path):
    p = tmp_path / "secret.docx"
    p.write_bytes(b"\x13\x37DRMHEADER" + bytes(range(256)))
    r = route(p)
    assert r.group == Group.REJECT and "DRM" in r.reject_reason


def test_empty_file(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    assert route(p).reject_reason == "빈 파일"


def test_missing_file_does_not_raise(tmp_path):
    r = route(tmp_path / "없음.pdf")
    assert r.group == Group.REJECT


# ---------------------------------------------------------------- Docling에 넘길 값 (형식 고정·인코딩)
def test_csv_cp949_encoding_reported(tmp_path):
    # Docling은 모르면 cp1252로 읽어 한글이 깨진 채 success를 낸다 → 라우터가 cp949를 알려 줘야 함
    p = tmp_path / "금리표.csv"
    p.write_bytes("상품,금리\n신용대출,5.1\n".encode("cp949"))
    r = route(p)
    assert (r.encoding, r.docling_ext) == ("cp949", ".csv")


def test_csv_ascii_head_then_cp949(tmp_path):
    # 앞 4KB는 영문만, 뒤에 한글(cp949) → 앞부분만 보면 UTF-8로 오판하므로 전체를 봐야 함
    p = tmp_path / "rates.csv"
    p.write_bytes(("code,rate\n" + "A001,5.1\n" * 600).encode() + "B002,신용대출\n".encode("cp949"))
    assert route(p).encoding == "cp949"


def test_csv_unknown_encoding_rejected(tmp_path):
    # 앞 4KB는 텍스트로 통과하지만 뒤가 UTF-8도 CP949도 아니면(여기선 Latin-1) 조용히 깨진 글자 대신 거부
    p = tmp_path / "x.csv"
    p.write_bytes(b"name,value\n" + b"a,1\n" * 1200 + b"caf\xe9 cr\xe8me,2\n")
    r = route(p)
    assert r.format == "text_encoding" and r.group == Group.REJECT


def test_eml_without_extension_gets_docling_ext(tmp_path):
    # 확장자 없는 메일은 Docling이 건너뜀(skipped) → 핸들러가 .eml 이름으로 넘기게 알려 줌
    p = tmp_path / "mail"
    p.write_text("From: a@example.com\nTo: b@example.com\nSubject: 심사 요청\n\n본문", encoding="utf-8")
    assert route(p).docling_ext == ".eml"


@needs_samples
def test_digital_pdf_keeps_ocr_on_default_mode():
    # 디지털 PDF도 OCR은 켠 채(기본 모드) 넘긴다: 글자+그림 섞인 쪽의 그림 글자를 잃지 않게
    r = route(SAMPLES / "law_go_kr/대부업등감독규정_본문_2026-47호.pdf")
    plan = r.pdf.ocr_plan
    assert plan.do_ocr and not plan.full_page_pages


# ---------------------------------------------------------------- PDF 쪽 판정 (순수 함수)
@pytest.mark.parametrize("visible,invisible,image,state", [
    ([], 0, 1.0, "empty"),                                        # 스캔 쪽
    ([], 12, 1.0, "invisible"),                                   # 스캐너가 OCR해 둔 보이지 않는 글자만
    (["제1조(목적) 이 규정은 대부업 등의 감독에 필요한 사항을 정한다."], 0, 0.0, "ok"),
    (["제1조(목적) 이 규정은 대부업 등의 감독에 필요한 사항을 정한다."], 0, 0.4, "image"),
    (["\x01\x02\x03�� 깨진 글자층 예시입니다 \x04\x05\x06\x07"] * 5, 0, 0.0, "broken"),
    (["Les co?ts et frais sont acquitt?s pour couvrir la d?pense"] * 3, 0, 0.0, "qmark"),
    # 오탐 방지: 한 종류뿐인 기호 글꼴 문자·구분자 제어문자는 깨짐이 아님
    ([" 제1호  자기자본  위험가중자산 "] * 6, 0, 0.0, "ok"),
    (["1) \x07Debt owed by households is referred to as household credit"] * 8, 0, 0.0, "ok"),
    # 글꼴 대응이 통째로 PUA로 밀린 경우(종류가 많음)는 깨짐
    (["".join(chr(0xE000 + i) for i in range(k, k + 30)) for k in range(0, 150, 30)], 0, 0.0, "broken"),
])
def test_classify_pdf_page(visible, invisible, image, state):
    assert classify_pdf_page(visible, invisible, image) == state


def test_pdf_signature_not_at_start():
    # 규격상 %PDF- 는 처음 1KB 안이면 된다
    assert sniff(b"\x00" * 100 + b"%PDF-1.4") == "pdf"


# ---------------------------------------------------------------- 3) classify_ole
def test_ole_encrypted_ooxml_before_legacy():
    fmt, _ = classify_ole(["EncryptedPackage", "EncryptionInfo", "\x06DataSpaces/Version"], b"")
    assert fmt == "ooxml_encrypted"


@pytest.mark.parametrize("stream,fmt", [("WordDocument", "legacy_doc"), ("Workbook", "legacy_xls"), ("PowerPoint Document", "legacy_ppt")])
def test_ole_legacy_office(stream, fmt):
    assert classify_ole([stream, "\x05SummaryInformation"], b"")[0] == fmt


def test_ole_msg():
    assert classify_ole(["__substg1.0_0037001F", "__properties_version1.0"], b"")[0] == "msg"


def _hwp_header(props: int) -> bytes:
    return b"HWP Document File".ljust(32, b"\x00") + (0x05000000).to_bytes(4, "little") + props.to_bytes(4, "little")


@pytest.mark.parametrize("props,fmt", [
    (0b001, "hwp"),
    (0b011, "hwp_password"),          # 실제 암호 샘플의 값(0x3)
    (0b101, "hwp_distribution"),
    (0x101, "hwp_password"),          # 공인인증서 암호화
    (0x011, "hwp_drm"),
    (0x401, "hwp_drm"),
])
def test_ole_hwp_flags(props, fmt):
    assert classify_ole(["FileHeader", "BodyText/Section0"], _hwp_header(props))[0] == fmt


def test_ole_hwp_viewtext_means_distribution():
    # 속성 비트가 없어도 본문이 ViewText에 있으면 배포용
    fmt, ev = classify_ole(["FileHeader", "ViewText/Section0"], _hwp_header(0b001))
    assert fmt == "hwp_distribution" and "ViewText stream" in ev
