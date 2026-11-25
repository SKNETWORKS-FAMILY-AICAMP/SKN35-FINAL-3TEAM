

def test_attachment_reader_is_added_when_description_mentions_files():
    from contracts.harness import ToolSpec
    from services.agent_draft import fallback_draft, with_attachment_reader
    specs = [ToolSpec("loans_maturing", "만기 조회", effect="read"), ToolSpec("parse_doc", "업로드 문서 읽기", effect="read")]
    assert with_attachment_reader(["loans_maturing"], "엑셀로 받은 대출 목록을 보고 만기를 정리", specs) == ["loans_maturing", "parse_doc"]
    assert with_attachment_reader(["loans_maturing"], "만기 대출을 알려 주는 에이전트", specs) == ["loans_maturing"]
    assert with_attachment_reader([], "첨부한 신청서 검토", specs[:1]) == []          # 첨부 도구가 없는 서버
    assert "parse_doc" in fallback_draft("고객이 보낸 서류 파일을 요약하는 에이전트", specs).tools


def test_templates_that_take_files_have_the_attachment_reader():
    from pathlib import Path
    import yaml
    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/agent_templates.yaml").read_text(encoding="utf-8"))
    tools = {t["id"]: t["tools"] for t in data["templates"]}
    for template_id in ("uc01-regulation", "doc-deadline", "uc03-maturity-alert", "uc03-maturity-report", "uc05-maturity-notice"):
        assert "parse_doc" in tools[template_id], template_id
