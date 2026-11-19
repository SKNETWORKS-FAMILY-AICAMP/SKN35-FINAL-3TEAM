"""공통 테스트를 돌릴 구현 목록.

각자 아래 경로에 패키지를 만들고 함수를 내보내면 자동으로 테스트 대상이 된다.
  harness/<이름>/__init__.py     →  create_harness(...)   (contracts.harness.HarnessFactory)
  permission/<이름>/__init__.py  →  create_checker(...)   (contracts.permission.CheckerFactory)

아직 패키지가 없으면 해당 구현의 테스트는 skip 된다.
환경변수로 덮어쓸 수도 있다 (쉼표 구분):
  HARNESS_IMPLS=harness.son:create_harness pytest
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any

import pytest

# 구현 담당별 패키지 (브랜치 이름과 같게)
HARNESS_IMPLS = [
    "harness.son:create_harness",
    "harness.dy:create_harness",
]

CHECKER_IMPLS = [
    "permission.son:create_checker",
    "permission.dy:create_checker",
]


def impl_ids(env_name: str, defaults: list[str]) -> list[str]:
    raw = os.environ.get(env_name)
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return defaults


def load_factory(spec: str) -> Callable[..., Any]:
    """'pkg.module:func' 를 불러온다. 패키지 자체가 없으면 skip, 그 외 에러는 그대로 실패."""
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name and module_name.startswith(e.name):
            pytest.skip(f"{module_name} 아직 없음")
        raise
    return getattr(module, attr)
