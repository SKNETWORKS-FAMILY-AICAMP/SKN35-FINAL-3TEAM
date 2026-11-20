# -*- coding: utf-8 -*-
"""파서 진입점(knowledge.ingestion.parsers.pipeline) — 모델 없이 도는 경로만 (HWPX·DOCX·거절·실패·저장). PDF·이미지는 동등성 스크립트로 따로 확인."""
import io
import json
import zipfile

import pytest

from knowledge.ingestion.parsers.pipeline import DocumentParser, ParseResult

HP = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"'


def _hwpx(encrypted=False) -> bytes:
    """제목 1개(개요 1 스타일) + 본문 1개 + 1x2 표가 든 가장 작은 HWPX."""
    header = f'<hh:head {HP}><hh:styles><hh:style id="1" name="개요 1"/></hh:styles></hh:head>'
    section = (f'<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" {HP}>'
               '<hp:p styleIDRef="1"><hp:run><hp:t>제1장 총칙</hp:t></hp:run></hp:p>'
               '<hp:p styleIDRef="0"><hp:run><hp:t>이 규정은 개인정보 처리 기준을 정한다.</hp:t></hp:run></hp:p>'
               '<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2"><hp:tr>'
               '<hp:tc><hp:subList><hp:p><hp:run><hp:t>구분</hp:t></hp:run></hp:p></hp:subList>'
               '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
               '<hp:tc><hp:subList><hp:p><hp:run><hp:t>내용</hp:t></hp:run></hp:p></hp:subList>'
               '<hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
               '</hp:tr></hp:tbl></hp:run></hp:p></hs:sec>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/header.xml", header)
        z.writestr("Contents/section0.xml", section)
        if encrypted:
            z.writestr("META-INF/manifest.xml", "<manifest><encryption-data/></manifest>")
    return buf.getvalue()


@pytest.fixture(scope="module")
def parser():
    return DocumentParser()


def test_hwpx_bytes_parsed_with_heading_path_and_table(parser):
    r = parser.parse(_hwpx(), file_name="규정.hwpx")
    assert r.status == "ok" and r.db_status == "ready" and r.route["handler"] == "hwpx_extract"
    body = next(e for e in r.elements if e["text"].startswith("이 규정은"))
    assert body["heading_path"] == ["제1장 총칙"]
    tab = next(e for e in r.elements if "table" in e)
    assert tab["table"]["rows"] == 1 and tab["table"]["cols"] == 2 and tab["table"]["source"] == "원본(HWPX)"
    assert [c["text"] for c in tab["table"]["cells"]] == ["구분", "내용"]
    assert "제1장 총칙" in r.markdown and "| 구분" in r.markdown


def test_encrypted_hwpx_rejected(parser):
    r = parser.parse(_hwpx(encrypted=True), file_name="암호.hwpx")
    assert r.status == "rejected" and r.db_status == "failed" and "암호" in r.reason


def test_unknown_bytes_rejected_not_raised(parser):
    r = parser.parse(b"\x00\x01\x02 not a document", file_name="이상한.bin")
    assert r.status == "rejected" and r.reason and r.route["handler"] == "reject"


def test_missing_file_fails_not_raised(parser, tmp_path):
    r = parser.parse(tmp_path / "없는파일.pdf")
    assert r.status in ("failed", "rejected") and r.db_status == "failed" and r.reason


def test_docx_goes_through_docling(parser, tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("개요", level=1)
    d.add_paragraph("본 지침은 내부 보고 절차를 정한다.")
    t = d.add_table(rows=2, cols=2)
    for i, txt in enumerate(["항목", "기한", "보고", "매월"]):
        t.cell(i // 2, i % 2).text = txt
    f = tmp_path / "지침.docx"
    d.save(f)
    r = parser.parse(f)
    assert r.status == "ok" and r.route["format"] == "docx"
    tab = next(e for e in r.elements if "table" in e)
    assert (tab["table"]["rows"], tab["table"]["cols"]) == (2, 2) and tab["table"]["source"] == "Docling(docx)"
    assert any(e["text"].startswith("본 지침") and e["heading_path"] == ["개요"] for e in r.elements)


def test_save_beside_writes_parsed_json(parser, tmp_path):
    src = tmp_path / "규정.hwpx"
    src.write_bytes(_hwpx())
    r = parser.parse(src)
    out = r.save_beside(src, document_version_id=42)
    assert out == tmp_path / "parsed" / "42.json"
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["document_version_id"] == 42 and d["status"] == "ok" and d["db_status"] == "ready"
    assert d["schema"].startswith("parse_result/") and "docling_document" in d and d["elements"]


def test_failed_result_has_no_document():
    r = ParseResult("failed", "테스트")
    d = r.to_dict(document_version_id=1)
    assert d["db_status"] == "failed" and "docling_document" not in d
