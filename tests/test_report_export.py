from io import BytesIO
from types import SimpleNamespace

from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.reports import router
from services.users import UserDirectory
from tests.test_market_tools import KIM


def example():
    return {"schema_version": 1, "title": "금리·환율 보고서", "report_id": "a" * 32, "base_date": "2026-11-18",
            "template": {"id": "test", "version": 2}, "blocks": [
                {"type": "notice", "text": "테스트 자료입니다."},
                {"type": "heading", "level": 1, "text": "금리·환율 보고서"},
                {"type": "field", "label": "기준일", "value": "2026-11-18"},
                {"type": "paragraph", "text": "확인한 수치만 정리했습니다."},
                {"type": "table", "columns": ["통화", "환율"], "rows": [["USD", "1,338.80"]]},
                {"type": "list", "items": ["출처: 한국은행 ECOS", "기록 재생 — 실시간 아님"]}]}


def client():
    app = FastAPI()
    app.include_router(router)
    app.state.services = SimpleNamespace(users=UserDirectory({"kim": KIM}))
    return TestClient(app)


def test_docx_download_preserves_json_fields_tables_and_sources():
    with client() as http:
        response = http.post("/api/reports/export/docx", headers={"X-User-Id": "kim"}, json=example())
    assert response.status_code == 200
    assert '.docx"' in response.headers["content-disposition"]
    doc = Document(BytesIO(response.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "금리·환율 보고서" in text and "기준일: 2026-11-18" in text
    assert "출처: 한국은행 ECOS" in text
    assert doc.tables[0].cell(1, 1).text == "1,338.80"


def test_export_requires_login_and_rejects_invalid_table_and_version():
    with client() as http:
        assert http.post("/api/reports/export/docx", json=example()).status_code == 401
        malformed = example()
        malformed["blocks"][4]["rows"] = [["one cell"]]
        assert http.post("/api/reports/export/docx", headers={"X-User-Id": "kim"}, json=malformed).status_code == 422
        assert http.post("/api/reports/export/docx", headers={"X-User-Id": "kim"}, json={**example(), "schema_version": 99}).status_code == 422
