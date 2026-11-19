"""권한 체커 공통 테스트: 모든 구현 × 모든 케이스.

실행:  uv run pytest tests/test_permission_contract.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.permission import Decision, PermissionChecker, PolicyRule, Principal, ToolCall
from tests.implementations import CHECKER_IMPLS, impl_ids, load_factory

FIXTURE = Path(__file__).parent.parent / "data/fixtures/permission_scenarios.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))

RULES = [
    PolicyRule(
        id=r["id"],
        tool=r["tool"],
        decision=Decision(r["decision"]),
        roles=tuple(r.get("roles", [])),
        reason=r.get("reason", ""),
    )
    for r in DATA["rules"]
]
DEFAULT = Decision(DATA["default"])
CASES = DATA["cases"]


@pytest.fixture(params=impl_ids("CHECKER_IMPLS", CHECKER_IMPLS))
def checker(request) -> PermissionChecker:
    factory = load_factory(request.param)
    c = factory(rules=RULES, default=DEFAULT)
    assert isinstance(c, PermissionChecker), "create_checker 가 PermissionChecker 를 돌려주지 않음"
    return c


def make_call(tool: str, roles: list[str]) -> ToolCall:
    return ToolCall("c1", tool, {}, Principal("tester", tuple(roles)), "session-1")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_case(checker, case):
    result = await checker.check(make_call(case["tool"], case["roles"]))
    assert result.decision == Decision(case["decision"])
    assert result.rule_id == case["rule_id"]
    if result.decision != Decision.ALLOW:
        assert result.reason, "DENY/ASK 인데 reason 이 비어 있음"


@pytest.mark.parametrize("default", list(Decision))
async def test_empty_rules_use_default(default):
    for spec in impl_ids("CHECKER_IMPLS", CHECKER_IMPLS):
        factory = load_factory(spec)
        result = await factory(rules=[], default=default).check(make_call("search", ["user"]))
        assert result.decision == default
        assert result.rule_id is None
