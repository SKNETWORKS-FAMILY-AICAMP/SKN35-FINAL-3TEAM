"""정책 YAML → PolicyRule 목록.

형식 (config/permissions.yaml):
    default: ask            # allow | deny | ask, 생략하면 env PERMISSION_DEFAULT_DECISION
    rules:
      - id: db-own-dept
        tool: query_db      # 툴 이름 또는 "*"
        decision: allow
        roles: [member]     # 생략 가능
        reason: "..."       # deny / ask 면 필수
        conditions:         # 생략 가능, 모두 참이어야 맞음
          - {attr: args.department_id, op: eq, value_from: principal.department_id}
          - {attr: principal.security_level, op: gte, value: 2}

규칙은 파일에 적힌 순서대로 매칭된다 (DB 로 옮기면 policy_rule.priority 순).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from contracts.permission import Condition, Decision, PolicyRule
from permission.checker import validate_rules

_RULE_KEYS = {"id", "tool", "decision", "roles", "reason", "conditions"}
_COND_KEYS = {"attr", "op", "value", "value_from"}


class PolicyError(ValueError):
    pass


def parse_policy(data: Any, default: Decision | str = Decision.ASK) -> tuple[list[PolicyRule], Decision]:
    if not isinstance(data, dict):
        raise PolicyError("정책 파일 최상위는 매핑(default, rules)이어야 함")
    default_decision = Decision(data.get("default", default))
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise PolicyError("rules 는 리스트여야 함")

    rules: list[PolicyRule] = []
    for i, raw in enumerate(raw_rules):
        where = f"rules[{i}]"
        if not isinstance(raw, dict):
            raise PolicyError(f"{where}: 매핑이어야 함")
        unknown = set(raw) - _RULE_KEYS
        if unknown:
            raise PolicyError(f"{where}: 알 수 없는 키 {sorted(unknown)}")
        for key in ("id", "tool", "decision"):
            if key not in raw:
                raise PolicyError(f"{where}: '{key}' 가 없음")
        decision = Decision(raw["decision"])
        reason = str(raw.get("reason", "") or "")
        if decision is not Decision.ALLOW and not reason:
            raise PolicyError(f"{where} ({raw['id']}): deny/ask 규칙은 reason 이 필요함")

        conditions = []
        for j, c in enumerate(raw.get("conditions") or []):
            if not isinstance(c, dict) or not {"attr", "op"} <= set(c) or set(c) - _COND_KEYS:
                raise PolicyError(f"{where}.conditions[{j}]: attr, op 와 value 또는 value_from 이 필요함")
            conditions.append(Condition(str(c["attr"]), c["op"], c.get("value"), c.get("value_from")))

        rules.append(
            PolicyRule(
                id=str(raw["id"]),
                tool=str(raw["tool"]),
                decision=decision,
                roles=tuple(str(r) for r in raw.get("roles") or ()),
                reason=reason,
                conditions=tuple(conditions),
            )
        )

    try:
        validate_rules(rules)
    except ValueError as e:
        raise PolicyError(str(e)) from e
    return rules, default_decision


def load_policy(path: str | Path, default: Decision | str = Decision.ASK) -> tuple[list[PolicyRule], Decision]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_policy(yaml.safe_load(text), default)


async def aload_policy(path: str | Path, default: Decision | str = Decision.ASK) -> tuple[list[PolicyRule], Decision]:
    """파일 I/O 를 이벤트 루프 밖에서 한다 (NFR-04)."""
    return await asyncio.to_thread(load_policy, path, default)
