"""퇴사(삭제)한 직원의 메일 보관 관리 — 관리자용 CLI.

직원(users)이 삭제되면 DB 트리거(alembic 0006)가 그 직원의 메일을 mail_archive 로 옮긴다.
에이전트(MCP 툴)는 이 표를 읽을 수 없고, 관리자가 이 명령으로만 본다. 플랫폼 PostgreSQL (GMAIL_DATABASE_URL) 에서만 동작한다.

  uv run gmail-archive list                          보관된 직원 목록 (건수)
  uv run gmail-archive search <직원> [검색어]        제목·보낸 사람·본문·받는 사람 검색
  uv run gmail-archive read <직원> <thread_id>       스레드 전체 읽기
  uv run gmail-archive purge <직원> --yes            영구 삭제 (보존 기간이 끝난 뒤만, 되돌릴 수 없음)

<직원> 은 former_user_id(숫자) 또는 삭제 당시 로그인 ID.
퇴사자의 Gmail 연결 토큰(.secrets/tokens/<로그인ID>.json)은 따로 지워야 한다 (이 명령은 건드리지 않음).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import Settings
from .store_pg import PostgresMailStore


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gmail-archive", description="퇴사한 직원의 메일 보관 관리 (관리자용)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="보관된 직원 목록")
    s = sub.add_parser("search", help="보관 메일 검색")
    s.add_argument("user")
    s.add_argument("query", nargs="*")
    s.add_argument("--limit", type=int, default=20)
    r = sub.add_parser("read", help="스레드 읽기")
    r.add_argument("user")
    r.add_argument("thread_id")
    d = sub.add_parser("purge", help="영구 삭제")
    d.add_argument("user")
    d.add_argument("--yes", action="store_true", help="되돌릴 수 없음을 확인했다")
    return p


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def run(store: PostgresMailStore, args: argparse.Namespace) -> int:
    if args.cmd == "list":
        _dump(await store.archive_users())
        return 0
    try:
        uid = await store.resolve_archive_user(args.user)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.cmd == "search":
        _dump([m.model_dump(mode="json") for m in await store.archive_search(uid, " ".join(args.query), args.limit)])
    elif args.cmd == "read":
        messages = await store.archive_thread(uid, args.thread_id)
        if not messages:
            print("해당 스레드가 보관돼 있지 않습니다")
            return 1
        _dump([m.model_dump(mode="json") for m in messages])
    elif args.cmd == "purge":
        if not args.yes:
            print(f"former_user_id={uid} 의 보관 메일을 영구 삭제합니다. 되돌릴 수 없습니다. 진행하려면 --yes 를 붙이세요")
            return 2
        print(f"삭제한 메일: {await store.archive_purge(uid)}통 (former_user_id={uid})")
    return 0


async def _main(argv: list[str]) -> int:
    settings = Settings.from_env()
    if not settings.database_url:
        print("보관 표는 플랫폼 PostgreSQL 에만 있습니다. GMAIL_DATABASE_URL 을 설정하세요")
        return 2
    store = PostgresMailStore(settings.database_url, max_size=2)
    try:
        return await run(store, _parser().parse_args(argv))
    finally:
        await store.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
