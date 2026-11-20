import asyncio
import json
from pathlib import Path

import httpx
import pytest

from contracts.permission import Principal, ToolCall
from main import create_app
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import load_policy
from services.container import AppServices
from services.users import UserDirectory
from tests.fakes import ScriptedLLM
from tools.get_review_case import GetReviewCaseTool
from tools.registry import SimpleToolRegistry
from tools.review_store import ReviewStore
from tools.submit_review_request import SubmitReviewRequestTool

KIM = Principal("kim", ("member",), {"dept_id": 10, "security_grade": 2})
PARK = Principal("park", ("member",), {"dept_id": 20, "security_grade": 2})
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    return ReviewStore(ROOT / "data/fixtures/review_cases.json", tmp_path / "requests.sqlite3")


def call(name, arguments, principal=KIM):
    return ToolCall("review-call", name, arguments, principal, "session")


async def test_lookup_access_and_input(store):
    tool = GetReviewCaseTool(store)
    result = await tool.run(call("get_review_case", {"case_id": "REVIEW-001"}))
    assert result.ok and result.data["missing_documents"] == ["납세증명서"]
    for principal in (PARK, Principal("low", (), {"dept_id": 10, "security_grade": 1}), Principal("missing")):
        denied = await tool.run(call("get_review_case", {"case_id": "REVIEW-001"}, principal))
        assert not denied.ok and "가상고객A" not in denied.content
    missing = await tool.run(call("get_review_case", {"case_id": "unknown"}))
    denied = await tool.run(call("get_review_case", {"case_id": "REVIEW-001"}, PARK))
    assert missing.content == denied.content
    for arguments in ({}, {"case_id": 1}, {"case_id": " "}, {"case_id": "REVIEW-001", "dept_id": 10}):
        assert not (await tool.run(call("get_review_case", arguments))).ok


async def test_concurrent_retry_and_persistence(store):
    tool = SubmitReviewRequestTool(store)
    args = {"case_id": "REVIEW-001", "notes": "보완서류 확인 요청"}
    first, second = await asyncio.gather(*(tool.run(call("submit_review_request", args)) for _ in range(2)))
    assert first.ok and second.ok
    assert first.data["request_id"] == second.data["request_id"]
    assert {first.data["already_registered"], second.data["already_registered"]} == {False, True}
    fresh_store = ReviewStore(store.case_path, store.request_path)
    result = await GetReviewCaseTool(fresh_store).run(call("get_review_case", {"case_id": "REVIEW-001"}))
    assert result.data["review_request"]["request_id"] == first.data["request_id"]
    assert not (await tool.run(call("submit_review_request", {**args, "notes": "다른 메모"}))).ok
    assert not (await tool.run(call("submit_review_request", args, PARK))).ok


async def test_bad_source_data_fails_safely(store, tmp_path):
    path = tmp_path / "bad.json"
    for content in ("{}", "[{}]", "not json"):
        path.write_text(content, encoding="utf-8")
        broken = ReviewStore(path, store.request_path)
        result = await GetReviewCaseTool(broken).run(call("get_review_case", {"case_id": "REVIEW-001"}))
        assert not result.ok
    assert not store.request_path.exists()


@pytest.mark.parametrize("approved", [True, False, None])
async def test_submission_through_api_approval(store, tmp_path, approved):
    rules, default = load_policy(ROOT / "config/permissions.yaml")
    # Verify even admins need approval for this write operation.
    checker = create_checker(rules=rules, default=default)
    from contracts.permission import Decision
    verdict = await checker.check(call("submit_review_request", {}, Principal("admin", ("admin",))))
    assert verdict.decision == Decision.ASK

    svc = AppServices(
        llm=ScriptedLLM([
            {"tool_calls": [{"id": "submit-1", "name": "submit_review_request",
                             "arguments": {"case_id": "REVIEW-001", "notes": "검토 요청"}}]},
            {"content": "등록 결과 확인"},
        ]),
        tools=SimpleToolRegistry([GetReviewCaseTool(store), SubmitReviewRequestTool(store)]),
        checker=checker,
        approvals=FutureApprovalHandler(0.1 if approved is None else 3),
        audit=JsonlAuditLogger(tmp_path / "audit.jsonl"),
        users=UserDirectory({"kim": KIM, "park": PARK}),
    )
    app = create_app(build=lambda: svc)
    app.state.services = svc
    headers = {"X-User-Id": "kim"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/tools", headers=headers)
        assert {tool["name"] for tool in response.json()} == {"get_review_case", "submit_review_request"}
        task = asyncio.create_task(client.post("/api/chat/messages", headers=headers, json={"content": "심사 요청 등록"}))
        for _ in range(100):
            pending = (await client.get("/api/approvals", headers=headers)).json()
            if pending:
                break
            await asyncio.sleep(0.005)
        assert pending
        assert store.get_case("REVIEW-001", KIM)["review_request"] is None
        assert (await client.post("/api/approvals/submit-1", headers={"X-User-Id": "park"}, json={"approved": True})).status_code == 404
        if approved is not None:
            assert (await client.post("/api/approvals/submit-1", headers=headers, json={"approved": approved})).status_code == 204
        body = (await task).json()
    persisted = store.get_case("REVIEW-001", KIM)["review_request"]
    assert bool(persisted) == (approved is True)
    assert body["stop_reason"] == ("completed" if approved is True else "denied")
    if approved is not True:
        assert body["denial"]["kind"] == ("timeout" if approved is None else "rejected")
    audit = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert audit["executed"] == (approved is True)
