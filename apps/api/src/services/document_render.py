"""문서(JSON) → PDF·DOCX. 모든 서류가 같은 문서 형식(blocks)을 쓴다.

문서 형식 (시장 리포트와 양식 문서 공통, config/report_templates/README.md):
    {"schema_version": 1, "title": str, "blocks": [...], "base_date"?: str, "template"?: {...}, "page"?: "portrait"|"landscape"}
blocks: heading(level, text) · paragraph(text) · notice(text) · field(label, value) · table(columns, rows) · list(items)
두 형식 모두 같은 JSON 에서 바로 만든다 (DOCX→PDF 변환에 Word·LibreOffice 가 필요 없게).
글자는 서식 명령으로 해석하지 않는다 (**, #, HTML 도 그대로 찍힌다).

PDF 한글 글꼴: PDF_FONT_PATH 환경변수(.ttf/.otf/.ttc), 없으면 OS 기본 한글 글꼴(FONT_CANDIDATES)을 찾는다.
.ttc(글꼴 모음)는 0번 글꼴을 쓴다.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

FORMATS = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
FONT_CANDIDATES = (
    "C:/Windows/Fonts/malgun.ttf",  # 윈도우: 맑은 고딕
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",  # 맥: 산돌고딕 Neo (0번이 Regular). AppleGothic.ttf 는 fpdf2 가 못 읽음
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",  # 리눅스 (fonts-nanum)
    "/usr/share/fonts/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # 리눅스 (fonts-noto-cjk)
)
DOCX_FONT = "맑은 고딕"
BLOCK_TYPES = {"heading", "paragraph", "notice", "field", "table", "list"}
MAX_BLOCKS = 500


class FontNotFound(RuntimeError):
    pass


class InvalidDocument(ValueError):
    pass


def find_font() -> Path:
    configured = os.environ.get("PDF_FONT_PATH", "").strip()
    for candidate in ([configured] if configured else []) + list(FONT_CANDIDATES):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FontNotFound("PDF 에 쓸 한글 글꼴(.ttf)을 찾지 못했습니다. PDF_FONT_PATH 에 글꼴 파일 경로를 넣어 주세요.")


def check_document(document: dict) -> dict:
    """저장된 문서라도 그리기 전에 형식을 다시 본다 (깨진 표·모르는 블록으로 파일이 잘못 나오지 않게)."""
    if not isinstance(document, dict) or document.get("schema_version") != 1 or not isinstance(document.get("title"), str):
        raise InvalidDocument("문서 형식(schema_version 1)이 아닙니다.")
    blocks = document.get("blocks")
    if not isinstance(blocks, list) or not 0 < len(blocks) <= MAX_BLOCKS:
        raise InvalidDocument("문서 블록이 비었거나 너무 많습니다.")
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in BLOCK_TYPES:
            raise InvalidDocument(f"모르는 블록 종류입니다: {kind}")
        if kind == "table":
            columns, rows = block.get("columns"), block.get("rows")
            if not columns or not isinstance(rows, list) or any(len(row) != len(columns) for row in rows):
                raise InvalidDocument("표의 열 수가 맞지 않습니다.")
    return document


def file_name(document: dict, fmt: str) -> str:
    base = document["title"] + (f"_{document['base_date']}" if document.get("base_date") else "")
    safe = "".join(ch if ch.isalnum() or ch in " -_·" else "_" for ch in base).strip()
    return f"{safe or 'document'}.{fmt}"


def _landscape(document: dict) -> bool:
    return document.get("page") == "landscape"


def render_pdf(document: dict) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    check_document(document)
    pdf = FPDF(orientation="L" if _landscape(document) else "P", format="A4")
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_font("ko", "", str(find_font()))
    pdf.set_title(document["title"])
    pdf.add_page()
    width = pdf.epw
    sizes = {1: 17, 2: 12.5, 3: 10.5}

    def text(value, size=9.5, color=(23, 32, 51)):
        pdf.set_font("ko", size=size)
        pdf.set_text_color(*color)
        pdf.multi_cell(width, size * 0.62, str(value), new_x="LMARGIN", new_y="NEXT")

    for block in document["blocks"]:
        kind = block["type"]
        if kind == "heading":
            level = block.get("level", 2)
            pdf.ln(2 if level > 1 else 0)
            text(block["text"], sizes.get(level, 11), (35, 59, 97))
            if level == 1:
                pdf.set_draw_color(35, 59, 97)
                pdf.line(pdf.l_margin, pdf.get_y() + 1, pdf.l_margin + width, pdf.get_y() + 1)
                pdf.ln(3)
        elif kind == "paragraph":
            text(block["text"])
        elif kind == "notice":
            text(block["text"], 9, (82, 105, 135))
        elif kind == "field":
            text((block.get("label") + ": " if block.get("label") else "") + str(block.get("value", "")))
        elif kind == "list":
            for item in block["items"]:
                text("• " + str(item))
        else:  # table
            widths = tuple(block.get("widths") or [1] * len(block["columns"]))
            pdf.set_font("ko", size=8.8)
            pdf.set_text_color(23, 32, 51)
            pdf.set_draw_color(203, 211, 223)
            with pdf.table(text_align="LEFT", line_height=5, padding=1.2, col_widths=widths, repeat_headings=1,
                           headings_style=FontFace(fill_color=(238, 242, 247))) as table:
                row = table.row()
                for column in block["columns"]:
                    row.cell(str(column))
                for values in block["rows"]:
                    row = table.row()
                    for value in values:
                        row.cell(str(value))
        pdf.ln(2.5)
    return bytes(pdf.output())


def render_docx(document: dict) -> bytes:
    # dy 의 routers/reports.py build_docx 를 옮겨 왔다 (머리글 행 반복, 행 나눔 방지, eastAsia 글꼴)
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor

    check_document(document)
    doc = Document()
    page = doc.sections[0]
    if _landscape(document):
        page.orientation = WD_ORIENT.LANDSCAPE
        page.page_width, page.page_height = Mm(297), Mm(210)
    else:
        page.page_width, page.page_height = Mm(210), Mm(297)
    page.top_margin = page.bottom_margin = Mm(18)
    page.left_margin = page.right_margin = Mm(16)
    doc.core_properties.title = document["title"]
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "List Bullet"):
        style = doc.styles[name]
        style.font.name = DOCX_FONT
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), DOCX_FONT)  # 한글은 eastAsia 글꼴을 따로
    normal = doc.styles["Normal"]
    normal.font.size = Pt(9.5)
    normal.paragraph_format.space_after = Pt(7)
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "heading":
            doc.add_heading(block["text"], level=block.get("level", 2))
        elif kind == "field":
            p = doc.add_paragraph()
            p.add_run(block.get("label", "") + (": " if block.get("label") else "")).bold = True
            p.add_run(str(block.get("value", "")))
        elif kind == "table":
            table = doc.add_table(rows=1, cols=len(block["columns"]))
            table.style = "Table Grid"
            table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))  # 쪽이 넘어가도 머리글 반복
            for cell, value in zip(table.rows[0].cells, block["columns"]):
                cell.text = str(value)
                for run in cell.paragraphs[0].runs:
                    run.bold = True
            for values in block["rows"]:
                row = table.add_row()
                for cell, value in zip(row.cells, values):
                    cell.text = str(value)
                row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
            doc.add_paragraph()
        elif kind == "list":
            for item in block["items"]:
                doc.add_paragraph(str(item), style="List Bullet")
        else:
            p = doc.add_paragraph(block["text"])
            if kind == "notice":
                for run in p.runs:
                    run.font.color.rgb = RGBColor.from_string("526987")
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def render(document: dict, fmt: str) -> bytes:
    if fmt == "pdf":
        return render_pdf(document)
    if fmt == "docx":
        return render_docx(document)
    raise ValueError(f"지원하지 않는 형식입니다: {fmt}")
