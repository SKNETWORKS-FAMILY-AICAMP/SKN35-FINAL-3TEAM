"""사용자 화면에 이미 제공된 구조화 JSON을 DOCX로 변환한다. 파일 조회/저장은 하지 않는다."""
from io import BytesIO
from typing import Literal, Annotated

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.permission import Principal
from routers.deps import current_user

router = APIRouter(prefix="/api/reports", tags=["reports"])
Text = Annotated[str, Field(max_length=20000)]
Cell = Annotated[str, Field(max_length=4000)]


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["heading", "paragraph", "notice", "field", "table", "list"]
    text: Text = ""
    level: int = Field(default=2, ge=1, le=3)
    label: str = Field(default="", max_length=120)
    value: Text = ""
    columns: list[Cell] = Field(default_factory=list, max_length=24)
    rows: list[list[Cell]] = Field(default_factory=list, max_length=500)
    items: list[Text] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def rectangular_table(self):
        if self.type == "table" and (not self.columns or any(len(row) != len(self.columns) for row in self.rows)):
            raise ValueError("표의 열 수가 일치해야 합니다.")
        return self


class ReportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    title: str = Field(max_length=120)
    base_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    report_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    template: dict = Field(default_factory=dict)
    blocks: list[Block] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def bounded_document(self):
        if len(self.model_dump_json()) > 400000:
            raise ValueError("보고서 내용은 400,000자 이내여야 합니다.")
        return self


def build_docx(report: ReportDocument) -> bytes:
    doc = Document()
    page = doc.sections[0]
    page.page_width, page.page_height = Mm(210), Mm(297)
    page.top_margin = page.bottom_margin = Mm(18)
    page.left_margin = page.right_margin = Mm(16)
    doc.core_properties.title = report.title
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "List Bullet"):
        style = doc.styles[name]
        style.font.name = "맑은 고딕"
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "맑은 고딕")
    normal = doc.styles["Normal"]
    normal.font.size = Pt(9.5)
    normal.paragraph_format.space_after = Pt(7)
    for block in report.blocks:
        if block.type == "heading":
            doc.add_heading(block.text, level=block.level)
        elif block.type == "field":
            p = doc.add_paragraph()
            p.add_run(block.label + (": " if block.label else "")).bold = True
            p.add_run(block.value)
        elif block.type == "table":
            table = doc.add_table(rows=1, cols=len(block.columns))
            table.style = "Table Grid"
            repeat = OxmlElement("w:tblHeader")
            table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for cell, text in zip(table.rows[0].cells, block.columns):
                cell.text = text
                for run in cell.paragraphs[0].runs:
                    run.bold = True
            for values in block.rows:
                row = table.add_row()
                for cell, text in zip(row.cells, values):
                    cell.text = text
                row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
            doc.add_paragraph()
        elif block.type == "list":
            for item in block.items:
                doc.add_paragraph(item, style="List Bullet")
        else:
            p = doc.add_paragraph(block.text)
            if block.type == "notice":
                for run in p.runs:
                    run.font.color.rgb = RGBColor.from_string("526987")
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


@router.post("/export/docx")
def export_docx(report: ReportDocument, user: Principal = Depends(current_user)):
    return Response(content=build_docx(report),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="report_{report.base_date}_{report.report_id[:8]}.docx"'})
