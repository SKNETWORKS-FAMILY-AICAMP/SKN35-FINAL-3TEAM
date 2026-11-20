import json

import httpx
import pytest

from contracts.permission import Principal, ToolCall
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import load_policy
from services.container import AppServices
from services.uploads import UploadStore
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tests.knowledge.ingestion.parsers.test_pipeline import _hwpx
from tools.parse_document import ParseDocumentTool
from tools.registry import SimpleToolRegistry

KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
PARK = Principal("park", ("member",), {"dept_id": 20, "security_grade": 2})
HEADERS = {"X-User-Id": "kim"}


@pytest.fixture
def setup(tmp_path):
    uploads = UploadStore(tmp_path / "uploads")
    tool = ParseDocumentTool(uploads.directory, uploads=uploads)
    rules, default = load_policy("config/permissions.yaml")
    svc = AppServices(
        llm=ScriptedLLM([]), tools=SimpleToolRegistry([tool]),
        checker=create_checker(rules=rules, default=default),
        approvals=FutureApprovalHandler(1), audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory({"kim": KIM, "park": PARK}), uploads=uploads,
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    return app, svc, tool


async def test_upload_chat_parse_and_followup(setup):
    app, svc, _ = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        upload = await client.post("/api/uploads", headers=HEADERS, files={"file": ("업무지침.hwpx", _hwpx())})
        assert upload.status_code == 201
        metadata = upload.json()
        assert metadata["filename"] == "업무지침.hwpx" and metadata["file_id"] != metadata["filename"]
        svc.llm = ScriptedLLM([
            {"tool_calls": [{"id": "parse-1", "name": "parse_document", "arguments": {"file_id": metadata["file_id"]}}]},
            {"content": "개인정보 처리 기준과 표를 읽었습니다."},
            {"content": "앞서 첨부하신 문서입니다."},
        ])
        agent = (await client.post("/api/agents", headers=HEADERS, json={
            "name": "파서", "system_prompt": "첨부를 읽으세요.", "tools": ["parse_document"],
        })).json()
        response = await client.post("/api/chat/messages", headers=HEADERS, json={
            "agent_id": agent["agent_id"], "file_ids": [metadata["file_id"]],
        })
        assert response.status_code == 200, response.text
        body = response.json()
        parsed = json.loads(body["tool_events"][0]["result"])
        assert parsed["filename"] == "업무지침.hwpx" and "제1장 총칙" in parsed["text"]
        assert body["sources"][0]["title"] == "업무지침.hwpx"
        first_messages = svc.llm.calls[0][0]
        assert metadata["file_id"] in first_messages[-1].content
        assert "parse_document" in first_messages[0].content
        followup = await client.post("/api/chat/messages", headers=HEADERS, json={
            "session_id": body["session_id"], "content": "이 문서 제목은?",
        })
        assert followup.status_code == 200
        assert any(metadata["file_id"] in message.content for message in svc.llm.calls[-1][0])


async def test_other_users_file_is_denied_by_chat_and_tool(setup):
    app, svc, tool = setup
    item = svc.uploads.create("kim", "규정.hwpx", _hwpx())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat/messages", headers={"X-User-Id": "park"}, json={
            "content": "읽어줘", "file_ids": [item["file_id"]],
        })
        assert response.status_code == 404
    result = await tool.run(ToolCall("c1", "parse_document", {"file_id": item["file_id"]}, PARK, "s1"))
    assert not result.ok
    assert svc.llm.calls == []


async def test_agent_without_parser_rejects_attachment(setup):
    app, svc, _ = setup
    item = svc.uploads.create("kim", "규정.hwpx", _hwpx())
    agent = svc.agents.create(KIM, name="일반", system_prompt="일반 응답", tools=())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat/messages", headers=HEADERS, json={
            "agent_id": agent.agent_id, "file_ids": [item["file_id"]],
        })
        assert response.status_code == 409
    assert svc.llm.calls == []


@pytest.mark.parametrize("filename,data,status", [("bad.exe", b"x", 422), ("empty.pdf", b"", 422), ("big.pdf", b"x" * (20*1024*1024+1), 413)])
async def test_upload_limits(setup, filename, data, status):
    app, _, _ = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/api/uploads", headers=HEADERS, files={"file": (filename, data)})
        assert result.status_code == status


async def test_upload_requires_user_and_chat_validates_lists(setup):
    app, _, _ = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/uploads", files={"file": ("a.pdf", b"x")})).status_code == 401
        for body in ({}, {"file_ids": ["a", "a"]}, {"file_ids": [str(n) for n in range(6)]}):
            assert (await client.post("/api/chat/messages", headers=HEADERS, json=body)).status_code == 422


def test_same_filenames_do_not_collide_and_ownership_survives_restart(tmp_path):
    store = UploadStore(tmp_path)
    first = store.create("kim", "규정.hwpx", _hwpx())
    second = store.create("park", "규정.hwpx", _hwpx())
    assert first["file_id"] != second["file_id"]
    reopened = UploadStore(tmp_path)
    assert reopened.get_owned(first["file_id"], "kim")["filename"] == "규정.hwpx"
    assert reopened.get_owned(first["file_id"], "park") is None


async def test_existing_unregistered_files_cannot_bypass_ownership(setup):
    _, svc, tool = setup
    svc.uploads.directory.mkdir(parents=True, exist_ok=True)
    (svc.uploads.directory / "shared.hwpx").write_bytes(_hwpx())
    result = await tool.run(ToolCall("c1", "parse_document", {"file_id": "shared.hwpx"}, KIM, "s1"))
    assert not result.ok
