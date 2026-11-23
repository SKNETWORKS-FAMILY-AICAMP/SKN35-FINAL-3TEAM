"""사용자 비밀번호 정하기 (DB 관리자용). 원문은 저장하지 않고 scrypt 해시만 users.password_hash 에 넣는다.

    uv run python scripts/db/set_password.py E1001          # 비밀번호를 두 번 물어본다 (화면에 안 보임)
    uv run python scripts/db/set_password.py E1001 E1002    # 여러 명에게 같은 비밀번호
    uv run python scripts/db/set_password.py --all          # DB 의 모든 사용자 (시연 준비용)

비밀번호를 물어보지 않게 하려면 환경변수 ARGUS_NEW_PASSWORD 에 넣는다 (.env 에 두지 말고 그 터미널에서만).
비밀번호를 바꾸면 그 사람의 기존 로그인 세션은 모두 끊고, 잠금·실패 횟수도 풀린다.
접속은 .env 의 DATABASE_URL.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "apps/api/src")]

from sqlalchemy import text  # noqa: E402

from scripts.db._env import load_dotenv  # noqa: E402

MIN_LENGTH = 8


def read_password() -> str:
    pw = os.environ.get("ARGUS_NEW_PASSWORD", "")
    if pw:
        source = "환경변수 ARGUS_NEW_PASSWORD"
    else:
        pw = getpass.getpass("새 비밀번호: ")
        if pw != getpass.getpass("한 번 더: "):
            sys.exit("두 비밀번호가 다릅니다.")
        source = "입력"
    if len(pw) < MIN_LENGTH:
        sys.exit(f"비밀번호는 {MIN_LENGTH}자 이상이어야 합니다 ({source}).")
    return pw


async def run(login_ids: list[str], all_users: bool) -> None:
    from db.engine import dispose_engine, session_scope
    from services.auth import hash_password

    password = read_password()
    async with session_scope() as s:
        if all_users:
            login_ids = [r[0] for r in (await s.execute(text("SELECT login_id FROM users ORDER BY id"))).all()]
        for login_id in login_ids:
            row = (await s.execute(text("""
                UPDATE users SET password_hash = :h, failed_login_count = 0, locked_until = NULL
                WHERE login_id = :l RETURNING id"""), {"h": hash_password(password), "l": login_id})).first()
            if row is None:
                print(f"없는 사용자: {login_id}")
                continue
            cut = await s.execute(text("UPDATE login_session SET revoked_at = now() "
                                       "WHERE user_id = :u AND revoked_at IS NULL"), {"u": row.id})
            print(f"{login_id}: 비밀번호 설정 (끊은 세션 {cut.rowcount or 0}개)")
        await s.commit()
    await dispose_engine()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("login_ids", nargs="*", help="users.login_id = 사번 (예: E1001)")
    ap.add_argument("--all", action="store_true", help="DB 의 모든 사용자")
    args = ap.parse_args()
    if not args.login_ids and not args.all:
        ap.error("사용자 아이디를 적거나 --all 을 주세요.")
    load_dotenv()
    asyncio.run(run(args.login_ids, args.all))


if __name__ == "__main__":
    main()
