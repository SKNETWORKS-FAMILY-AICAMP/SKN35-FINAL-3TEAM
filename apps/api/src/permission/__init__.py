"""권한 구현. contracts.permission.CheckerFactory 를 `create_checker` 로 내보낸다."""

from permission.checker import AbacChecker, create_checker

__all__ = ["AbacChecker", "create_checker"]
