"""개발용 사용자(config/dev_users.yaml)를 DB 의 dept · users · user_role 에 맞춰 넣는다.

로그인(COM-01)이 붙기 전까지 하네스는 yaml 의 사용자 속성(dept_id, security_grade)으로 권한을 판단한다.
문서 검색은 DB 의 owner_dept_id 와 사용자 dept_id 를 비교하므로, 부서 번호가 yaml 과 DB 에서 같아야 한다.
그래서 yaml 의 dept_id 를 그대로 dept.id 로 쓴다 (없으면 만들고, 있으면 둔다).

    uv run python scripts/db/seed_users.py                       # config/dev_users.yaml
    uv run python scripts/db/seed_users.py --users 다른.yaml

여러 번 돌려도 된다 (login_id 기준으로 있으면 속성만 맞춘다). 접속은 .env 의 DATABASE_URL.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "apps/api/src")]

import yaml  # noqa: E402
from sqlalchemy import text  # noqa: E402

from scripts.db._env import load_dotenv  # noqa: E402


async def seed(users_path: Path) -> None:
    from db.engine import dispose_engine, session_scope

    data = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
    users = data.get("users", [])
    async with session_scope() as s:
        for u in users:
            a = u.get("attributes", {})
            dept = a.get("dept_id")
            if type(dept) is not int:
                print(f"건너뜀 {u.get('user_id')}: dept_id 없음")
                continue
            await s.execute(text("INSERT INTO dept (id, name) OVERRIDING SYSTEM VALUE VALUES (:i, :n) "
                                 "ON CONFLICT (id) DO NOTHING"), {"i": dept, "n": f"부서 {dept}"})
            uid = await s.scalar(text(
                "INSERT INTO users (login_id, name, dept_id, position, security_grade) VALUES (:l, :l, :d, :p, :g) "
                "ON CONFLICT (login_id) DO UPDATE SET dept_id = EXCLUDED.dept_id, position = EXCLUDED.position, "
                "security_grade = EXCLUDED.security_grade RETURNING id"),
                {"l": u["user_id"], "d": dept, "p": a.get("position"), "g": a.get("security_grade", 1)})
            await s.execute(text("DELETE FROM user_role WHERE user_id = :u"), {"u": uid})
            for role in u.get("roles", []):
                await s.execute(text("INSERT INTO user_role (user_id, role) VALUES (:u, :r)"), {"u": uid, "r": role})
            print(f"{u['user_id']}: users.id={uid}, 부서 {dept}, 등급 {a.get('security_grade', 1)}, 역할 {u.get('roles', [])}")
        # 부서 번호를 직접 넣었으므로, 다음 자동 번호가 겹치지 않게 맞춘다
        await s.execute(text("SELECT setval(pg_get_serial_sequence('dept', 'id'), "
                             "GREATEST((SELECT max(id) FROM dept), 1))"))
        await s.commit()
    await dispose_engine()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--users", type=Path, default=ROOT / "config/dev_users.yaml")
    args = ap.parse_args()
    load_dotenv()
    asyncio.run(seed(args.users))


if __name__ == "__main__":
    main()
