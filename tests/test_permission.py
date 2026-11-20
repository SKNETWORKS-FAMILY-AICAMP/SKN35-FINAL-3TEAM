"""permission 구현 테스트 (정책 로더·감사 로그·승인). 계약 테스트는 test_permission_contract.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from contracts.permission import AuditRecord, Decision, PermissionResult, Principal, ToolCall
from permission import create_checker
from permission.approval import FutureApprovalHandler
from permission.audit import JsonlAuditLogger
from permission.policy import PolicyError, load_policy, parse_policy

ROOT = Path(__file__).parent.parent


def call(tool="query_db", args=None, user="u1", roles=("member",), attrs=None, call_id="c1"):
    return ToolCall(call_id, tool, args or {}, Principal(user, roles, attrs or {}), "s1")


# ── 정책 로더 ───────────────────────────────────────────────

def test_repo_policy_file_loads_and_works():
    rules, default = load_policy(ROOT / "config/permissions.yaml")
    assert default is Decision.ASK and rules


async def test_policy_yaml_drives_checker():
    rules, default = load_policy(ROOT / "config/permissions.yaml")
    checker = create_checker(rules=rules, default=default)
    own = await checker.check(call(args={"dept_id": 7}, attrs={"dept_id": 7}))
    other = await checker.check(call(args={"dept_id": 8}, attrs={"dept_id": 7}))
    assert (own.decision, own.rule_id) == (Decision.ALLOW, "db-own-dept")
    assert (other.decision, other.rule_id) == (Decision.DENY, "db-others")


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"rules": [{"id": "a", "tool": "x", "decision": "deny"}]}, "reason"),
        ({"rules": [{"id": "a", "tool": "x"}]}, "decision"),
        ({"rules": [{"id": "a", "tool": "x", "decision": "allow", "typo": 1}]}, "알 수 없는 키"),
        ({"rules": [{"id": "a", "tool": "x", "decision": "allow"}, {"id": "a", "tool": "y", "decision": "allow"}]}, "중복"),
        ({"rules": [{"id": "a", "tool": "x", "decision": "allow",
                     "conditions": [{"attr": "user.level", "op": "eq", "value": 1}]}]}, "경로"),
        ({"rules": [{"id": "a", "tool": "x", "decision": "allow",
                     "conditions": [{"attr": "principal.level", "op": "like", "value": 1}]}]}, "연산자"),
    ],
)
def test_bad_policy_fails_fast(data, msg):
    with pytest.raises(PolicyError, match=msg):
        parse_policy(data)


def test_bad_decision_value_fails():
    with pytest.raises(ValueError):
        parse_policy({"default": "maybe", "rules": []})


def test_default_reason_is_filled():
    checker = create_checker(rules=[], default=Decision.DENY)
    result = asyncio.run(checker.check(call(tool="anything")))
    assert result.reason


# ── 감사 로그 ───────────────────────────────────────────────

async def test_jsonl_audit_writes_one_line_per_record(tmp_path):
    path = tmp_path / "logs/audit.jsonl"
    logger = JsonlAuditLogger(path)
    c = call(args={"dept_id": 3}, attrs={"dept_id": 3, "security_grade": 2})
    await asyncio.gather(*[
        logger.log(AuditRecord(c, PermissionResult(Decision.ALLOW, rule_id="r"), executed=True))
        for _ in range(20)
    ])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    row = json.loads(lines[0])
    assert row["decision"] == "allow" and row["executed"] is True and row["rule_id"] == "r"
    assert row["principal_snapshot"] == {"user_id": "u1", "roles": ["member"], "dept_id": 3, "security_grade": 2}
    assert row["ts"].endswith("+00:00")  # UTC


# ── 승인 ───────────────────────────────────────────────────

async def test_approval_resolved_by_user():
    handler = FutureApprovalHandler(timeout_seconds=5)
    c = call(tool="send_email")
    task = asyncio.create_task(handler.request_approval(c, PermissionResult(Decision.ASK, "메일")))
    await asyncio.sleep(0)
    assert [p.call.call_id for p in handler.pending("s1")] == ["c1"]
    assert handler.resolve("c1", True, user_id="someone-else") is False  # 다른 사람은 승인 불가
    assert handler.resolve("c1", True, user_id="u1") is True
    assert await task == "approved"
    assert handler.pending() == []


async def test_approval_times_out_as_timeout_not_rejected():
    handler = FutureApprovalHandler(timeout_seconds=0.01)
    assert await handler.request_approval(call(), PermissionResult(Decision.ASK, "x")) == "timeout"
    assert handler.pending() == []
    assert handler.resolve("c1", True) is False


async def test_approval_on_request_callback_sees_pending():
    seen = []

    async def notify(p):
        seen.append(p.call.call_id)
        handler.resolve(p.call.call_id, False)

    handler = FutureApprovalHandler(timeout_seconds=5, on_request=notify)
    assert await handler.request_approval(call(call_id="c9"), PermissionResult(Decision.ASK, "x")) == "rejected"
    assert seen == ["c9"]


def test_audit_clips_long_arguments():
    from permission.audit import to_dict

    args = {"body": "가" * 600, "to": ["a@b.c"], "nested": {"x": "y" * 501}}
    rec = AuditRecord(
        call(tool="send_email", args=args),
        PermissionResult(Decision.ALLOW), executed=True, tool_ok=True,
    )
    row = to_dict(rec)
    assert row["arguments"]["body"] == "가" * 500 + "…(+100 chars)"
    assert row["arguments"]["nested"]["x"].endswith("…(+1 chars)") and row["arguments"]["to"] == ["a@b.c"]
    assert row["tool_ok"] is True
    assert len(rec.call.arguments["body"]) == 600  # 원본은 그대로
