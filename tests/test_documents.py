"""양식 JSON → fill_document → 공통 문서 형식(blocks) → PDF·DOCX 내려받기 (UC-03 만기 대출 정리표).
시장 리포트도 같은 형식·같은 내려받기를 쓴다 (tests/test_market_tools.py)."""

import io
import json
from pathlib import Path

import httpx
import pytest

from contracts.permission import Decision, Principal, ToolCall
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from services.container import AppServices
from services.document_render import InvalidDocument, check_document, find_font, render_docx
from services.documents import DocumentStore, load_forms
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.fill_document import FillDocumentTool
from tools.loans import LoanStore
from tools.registry import SimpleToolRegistry

ROOT = Path(__file__).resolve().parents[1]
KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
PARK = Principal("park", ("member",), {"dept_id": 20, "security_grade": 2})
LEE = Principal("lee", ("dept_head",), {"dept_id": 10, "security_grade": 3})
HAS_FONT = any(Path(p).is_file() for p in ("C:/Windows/Fonts/malgun.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                                           "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"))


@pytest.fixture
def store(tmp_path):
    return DocumentStore(tmp_path / "docs.sqlite3", load_forms(ROOT / "config/document_forms"))


@pytest.fixture
def tool(store):
    loans = LoanStore(ROOT / "data/fixtures/loans_demo.json")
    return FillDocumentTool(store, {"loans_maturing": lambda p, params: loans.maturing(p, params["days"])})


def fill(arguments, principal=KIM):
    return ToolCall("c1", "fill_document", arguments, principal, "session")


def after(document, heading):
    """제목 블록 바로 다음 블록 (그 칸의 내용)."""
    blocks = document["blocks"]
    index = next(i for i, b in enumerate(blocks) if b["type"] == "heading" and b["text"] == heading)
    return blocks[index + 1]


def app_with(store, tmp_path):
    svc = AppServices(
        llm=ScriptedLLM([]), tools=SimpleToolRegistry([]), checker=create_checker(rules=[], default=Decision.ASK),
        approvals=FutureApprovalHandler(1), audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory({"kim": KIM, "park": PARK}), documents=store,
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    return app


async def test_code_fills_table_and_model_fills_only_text(tool, store):
    assert "maturity-report" in tool.spec.description and "memo" in tool.spec.description
    result = await tool.run(fill({"form_id": "maturity-report", "params": {"days": 30},
                                  "fields": {"memo": "만기가 지난 홍*동 고객의 상환·연장 여부를 오늘 먼저 확인하세요."}}))
    assert result.ok and result.data["created"] and result.data["document_title"] == "2026-11-23 기준 30일 이내 만기 대출 정리표"
    record = store.get("kim", result.data["document_id"])
    document = check_document(record["document"])
    assert document["base_date"] == "2026-11-23" and document["page"] == "landscape" and record["has_customer_info"]
    assert document["blocks"][0] == {"type": "heading", "level": 1, "text": result.data["document_title"]}
    table = after(document, "대상 대출")
    assert table["columns"][0] == "구분" and len(table["rows"]) == 6 and table["rows"][0][5] == "5일 지남"
    assert [row[0] for row in after(document, "오늘 먼저 볼 대출")["rows"]] == ["홍*동", "(주)한빛정밀", "김*지"]
    assert after(document, "구분별 건수")["text"] == "만기 경과 1건 · 7일 이내 2건 · 8~30일 3건"
    assert after(document, "담당자 메모")["text"].startswith("만기가 지난")
    assert after(document, "유의 사항")["type"] == "notice"
    assert result.data["facts"]["total"] == 6 and result.data["facts"]["first_look"][0]["customer"] == "홍*동"
    assert store.get("park", result.data["document_id"]) is None


async def test_wrong_numbers_are_dropped_and_table_cannot_be_passed(tool, store):
    bad = await tool.run(fill({"form_id": "maturity-report", "params": {"days": 30}, "fields": {"memo": "47건이 급합니다."}}))
    assert bad.ok and bad.data["dropped"][0]["field"] == "memo"
    texts = [b.get("text") for b in store.get("kim", bad.data["document_id"])["document"]["blocks"]]
    assert "담당자 메모" not in texts  # 비운 문장 칸은 제목째 빠진다
    for arguments in ({"form_id": "maturity-report", "fields": {"items": "[]"}},
                      {"form_id": "maturity-report", "params": {"days": 365}},
                      {"form_id": "maturity-report", "params": {"officer": "park"}},
                      {"form_id": "nope"}):
        assert not (await tool.run(fill(arguments))).ok
    empty = await tool.run(fill({"form_id": "maturity-report"}, LEE))
    assert empty.ok and not empty.data["created"] and store.list("lee") == []


def test_bad_forms_are_rejected(tmp_path):
    (tmp_path / "f.json").write_text(json.dumps({"id": "x", "name": "x", "title": "x", "source": {"provider": "p"},
                                                  "sections": [{"key": "a", "label": "a", "fill": "data"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="from"):
        load_forms(tmp_path)


def example():
    """dy 의 시장 리포트 형식 예시 (이전 tests/test_report_export.py)."""
    return {"schema_version": 1, "title": "금리·환율 보고서", "base_date": "2026-11-18",
            "template": {"id": "test", "version": 2}, "blocks": [
                {"type": "notice", "text": "테스트 자료입니다."},
                {"type": "heading", "level": 1, "text": "금리·환율 보고서"},
                {"type": "field", "label": "기준일", "value": "2026-11-18"},
                {"type": "paragraph", "text": "확인한 수치만 정리했습니다. <script>x</script> **그대로**"},
                {"type": "table", "columns": ["통화", "환율"], "rows": [["USD", "1,338.80"]]},
                {"type": "list", "items": ["출처: 한국은행 ECOS", "기록 재생 — 실시간 아님"]}]}


def test_docx_keeps_fields_tables_sources_and_text_as_is():
    from docx import Document

    doc = Document(io.BytesIO(render_docx(example())))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "금리·환율 보고서" in text and "기준일: 2026-11-18" in text and "출처: 한국은행 ECOS" in text
    assert "<script>x</script> **그대로**" in text
    assert doc.tables[0].cell(1, 1).text == "1,338.80"


@pytest.mark.parametrize("change", [
    lambda d: d.update(schema_version=2),
    lambda d: d["blocks"][4].update(rows=[["one cell"]]),
    lambda d: d["blocks"].append({"type": "html", "text": "<b>"}),
    lambda d: d.update(blocks=[]),
])
def test_invalid_documents_are_not_rendered(change):
    document = example()
    change(document)
    with pytest.raises(InvalidDocument):
        check_document(document)


@pytest.mark.skipif(not HAS_FONT, reason="한글 글꼴 없음")
async def test_download_pdf_and_docx_only_for_owner(tool, store, tmp_path):
    from docx import Document
    import pypdfium2 as pdfium  # pypdf 는 이 글꼴의 한글을 \x00 섞어 읽는다 (PDF 는 정상)

    made = await tool.run(fill({"form_id": "maturity-report", "params": {"days": 30}, "fields": {"memo": "홍*동 고객을 먼저 확인하세요."}}))
    doc_id = made.data["document_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_with(store, tmp_path)), base_url="http://test") as client:
        kim = {"X-User-Id": "kim"}
        assert [d["document_id"] for d in (await client.get("/api/documents", headers=kim)).json()] == [doc_id]
        pdf = await client.get(f"/api/documents/{doc_id}/download?format=pdf", headers=kim)
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
        assert "filename*=UTF-8''" in pdf.headers["content-disposition"] and pdf.headers["cache-control"] == "no-store"
        reader = pdfium.PdfDocument(pdf.content)
        text = "".join(reader[i].get_textpage().get_text_range() for i in range(len(reader)))
        assert "만기 대출 정리표" in text and "(주)한빛정밀" in text and "D-3" in text and "5일 지남" in text
        docx = await client.get(f"/api/documents/{doc_id}/download?format=docx", headers=kim)
        body = Document(io.BytesIO(docx.content))
        assert body.tables[1].rows[0].cells[0].text == "구분" and len(body.tables[1].rows) == 7 and len(body.tables[0].rows) == 4
        assert any("홍*동 고객을 먼저" in p.text for p in body.paragraphs)
        assert (await client.get(f"/api/documents/{doc_id}/download?format=hwp", headers=kim)).status_code == 422
        park = {"X-User-Id": "park"}
        assert (await client.get(f"/api/documents/{doc_id}/download?format=pdf", headers=park)).status_code == 404
        assert (await client.get("/api/documents", headers=park)).json() == []
    assert find_font().is_file()


async def test_legacy_markdown_report_cannot_be_downloaded(store, tmp_path):
    store.put({"document_id": "c" * 32, "owner_id": "kim", "kind": "market_report", "title": "이전 보고서",
               "markdown": "# 이전 보고서"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_with(store, tmp_path)), base_url="http://test") as client:
        response = await client.get(f"/api/documents/{'c' * 32}/download?format=pdf", headers={"X-User-Id": "kim"})
    assert response.status_code == 409 and "이전 형식" in response.json()["detail"]
