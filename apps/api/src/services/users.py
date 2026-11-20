"""요청자(Principal) 확인.

로그인 방식(COM-01)이 정해지기 전까지 쓰는 개발용 구현이다.
클라이언트는 X-User-Id 헤더로 '누구인지'만 보내고, 역할·부서·보안등급 같은 속성은
서버가 config/dev_users.yaml 에서 채운다. 클라이언트가 보낸 속성은 절대 믿지 않는다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from contracts.permission import Principal


class UserDirectory:
    def __init__(self, users: dict[str, Principal]) -> None:
        self._users = users

    @classmethod
    def from_yaml(cls, path: str | Path) -> UserDirectory:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        users = {}
        for raw in data.get("users", []):
            uid = str(raw["user_id"])
            users[uid] = Principal(
                user_id=uid,
                roles=tuple(raw.get("roles", ())),
                attributes=dict(raw.get("attributes", {})),
            )
        return cls(users)

    def get(self, user_id: str) -> Principal | None:
        return self._users.get(user_id)

    def superior_of(self, p: Principal) -> Principal | None:
        """바로 위 관리자: 팀원 → 소속 부서장, 부서장 → admin. admin 은 위가 없다 (None).

        부서장이 없는 부서의 팀원은 admin 에게 올라간다.
        DB 가 붙으면 department.head_user_id 와 user_role 로 찾는다.
        """
        if "admin" in p.roles:
            return None
        dept = p.attributes.get("department_id")
        if "dept_head" not in p.roles:
            for u in self._users.values():
                if "dept_head" in u.roles and u.user_id != p.user_id and u.attributes.get("department_id") == dept:
                    return u
        return next((u for u in self._users.values() if "admin" in u.roles), None)
