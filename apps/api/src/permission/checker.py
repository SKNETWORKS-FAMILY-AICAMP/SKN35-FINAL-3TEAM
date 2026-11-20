"""ABAC 권한 체커: 규칙을 순서대로 보고 처음 맞는 규칙의 결정을 돌려준다.

매칭·조건 평가 규칙은 contracts/permission.py 의 '공통 정책 규칙 (ABAC)' 주석을 따른다.
판정은 순수 계산이라 I/O 가 없지만, 계약에 맞춰 check() 는 async 다.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from contracts.permission import (
    Condition,
    Decision,
    PermissionResult,
    PolicyRule,
    Principal,
    ToolCall,
)

_MISSING = object()
_ROOTS = ("principal", "args")


def _compare(op: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    def run(left: Any, right: Any) -> bool:
        try:
            return bool(op(left, right))
        except TypeError:  # "3" > 2 같은 비교 불가는 거짓
            return False

    return run


def _is_in(left: Any, right: Any) -> bool:
    return isinstance(right, (list, tuple, set, frozenset)) and left in right


def _contains(left: Any, right: Any) -> bool:
    if isinstance(left, str):
        return isinstance(right, str) and right in left
    if isinstance(left, (list, tuple, set, frozenset)):
        return right in left
    return False


OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": _compare(operator.gt),
    "gte": _compare(operator.ge),
    "lt": _compare(operator.lt),
    "lte": _compare(operator.le),
    "in": _is_in,
    "not_in": lambda left, right: isinstance(right, (list, tuple, set, frozenset)) and left not in right,
    "contains": _contains,
}


def _validate_path(path: str, rule_id: str) -> None:
    root, _, rest = path.partition(".")
    if root not in _ROOTS or not rest:
        raise ValueError(f"규칙 '{rule_id}': 경로 '{path}' 는 principal.<키> 또는 args.<키> 형식이어야 함")


def validate_rules(rules: Iterable[PolicyRule]) -> None:
    """잘못된 규칙은 서버가 뜰 때 바로 실패시킨다 (판정 중에 조용히 무시하지 않음)."""
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise ValueError(f"규칙 id '{rule.id}' 가 중복됨")
        seen.add(rule.id)
        for cond in rule.conditions:
            if cond.op not in OPS:
                raise ValueError(f"규칙 '{rule.id}': 알 수 없는 연산자 '{cond.op}'")
            _validate_path(cond.attr, rule.id)
            if cond.value_from is not None:
                _validate_path(cond.value_from, rule.id)


def resolve(path: str, principal: Principal, args: Mapping[str, Any]) -> Any:
    """'principal.dept_id' / 'args.target.id' 경로의 값. 없으면 _MISSING."""
    root, _, rest = path.partition(".")
    keys = rest.split(".")
    if root == "principal":
        head = keys[0]
        if head == "user_id":
            cur: Any = principal.user_id
        elif head == "roles":
            cur = principal.roles
        else:
            cur = principal.attributes.get(head, _MISSING)
        keys = keys[1:]
    else:
        cur = args
    for key in keys:
        if not isinstance(cur, Mapping) or key not in cur:
            return _MISSING
        cur = cur[key]
    return _MISSING if cur is None else cur


def condition_holds(cond: Condition, principal: Principal, args: Mapping[str, Any]) -> bool:
    left = resolve(cond.attr, principal, args)
    if left is _MISSING:
        return False
    if cond.value_from is not None:
        right = resolve(cond.value_from, principal, args)
        if right is _MISSING:
            return False
    else:
        right = cond.value
    return OPS[cond.op](left, right)


def rule_matches(rule: PolicyRule, call: ToolCall) -> bool:
    if rule.tool != "*" and rule.tool != call.tool_name:
        return False
    if rule.roles and not set(rule.roles) & set(call.principal.roles):
        return False
    args = call.arguments or {}
    return all(condition_holds(c, call.principal, args) for c in rule.conditions)


class AbacChecker:
    def __init__(self, rules: list[PolicyRule], default: Decision) -> None:
        validate_rules(rules)
        self._rules = tuple(rules)
        self._default = Decision(default)

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        return self._rules

    async def check(self, call: ToolCall) -> PermissionResult:
        for rule in self._rules:
            if rule_matches(rule, call):
                reason = rule.reason or (
                    "" if rule.decision is Decision.ALLOW else f"정책 규칙 '{rule.id}' 에 따라 {rule.decision.value}"
                )
                return PermissionResult(rule.decision, reason=reason, rule_id=rule.id)
        reason = "" if self._default is Decision.ALLOW else (
            f"'{call.tool_name}' 에 맞는 정책 규칙이 없어 기본값({self._default.value}) 적용"
        )
        return PermissionResult(self._default, reason=reason, rule_id=None)


def create_checker(*, rules: list[PolicyRule], default: Decision) -> AbacChecker:
    return AbacChecker(rules, default)
