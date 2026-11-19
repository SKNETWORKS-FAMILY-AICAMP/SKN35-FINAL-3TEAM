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
