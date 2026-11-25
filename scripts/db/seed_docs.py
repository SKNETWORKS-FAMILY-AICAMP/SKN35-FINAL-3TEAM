"""팀 공통 시연 문서(기업은행 내규 10개 + 권한 시연 문서 1개)를 내 DB 에 한 번에 넣는다.

    uv run python scripts/db/seed_docs.py            # 전부
    uv run python scripts/db/seed_docs.py --no-test  # 권한 시연용 부서 문서는 빼고

- 목록과 제목·공개 범위는 data/seed_docs/manifest.yaml. 원본 파일도 같은 폴더에 있다 (git 으로 같이 받음).
- 여러 번 돌려도 된다: 같은 제목·같은 파일이 이미 있으면 건너뛰고, 파일이 바뀌었으면 새 버전으로 올린다.
- 처음에는 문서마다 파싱·임베딩을 해서 CPU 로 10분 넘게 걸린다.
- 필요한 것: .env 의 DATABASE_URL · EMBEDDING_MODEL_PATH, 마이그레이션(alembic upgrade head), 직원(seed_users.py).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "apps/api/src")]

import yaml  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from scripts.db._env import load_dotenv  # noqa: E402

SEED_DIR = ROOT / "data" / "seed_docs"


def load_manifest(include_test: bool) -> list[dict]:
    data = yaml.safe_load((SEED_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    docs = [{**defaults, **d} for d in data["docs"]]
    return [d for d in docs if include_test or not d.get("test")]


async def plan(docs: list[dict]) -> list[tuple[dict, int | None]]:
    """문서마다 (항목, 새 버전으로 올릴 doc_id 또는 None=새 문서). 이미 같은 파일이 있는 것은 뺀다."""
    from db.engine import session_scope
    from db.models import Doc, DocVersion, User

    todo = []
    async with session_scope() as s:
        owners = {d["owner"] for d in docs}
        found = set((await s.execute(select(User.login_id).where(User.login_id.in_(owners)))).scalars())
        if missing := owners - found:
            raise SystemExit(f"직원 {sorted(missing)} 이 DB 에 없습니다. 먼저: uv run python scripts/db/seed_users.py")
        for d in docs:
            path = SEED_DIR / d["file"]
            if not path.is_file():
                raise SystemExit(f"파일 없음: {path} (git pull 을 했는지 확인)")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            doc_id = await s.scalar(select(func.min(Doc.id)).where(Doc.title == d["title"], Doc.status == "active"))
            if doc_id is not None:
                same = await s.scalar(select(DocVersion.id).where(DocVersion.doc_id == doc_id, DocVersion.sha256 == sha,
                                                                  DocVersion.status == "ready"))
                if same:
                    print(f"  있음  {d['title']}")
                    continue
            todo.append((d, doc_id))
    return todo


async def main_async(include_test: bool) -> None:
    from db.engine import dispose_engine, session_scope
    from db.models import Doc

    from scripts.db.register_doc import register

    docs = load_manifest(include_test)
    print(f"시연 문서 {len(docs)}개 확인")
    todo = await plan(docs)
    if todo:
        from knowledge.ingestion.embedders import KureEmbedder
        from knowledge.ingestion.parsers.pipeline import DocParser
        from storage.local import LocalStorage

        model = os.environ.get("EMBEDDING_MODEL_PATH")
        if not model:
            raise SystemExit(".env 에 EMBEDDING_MODEL_PATH(KURE-v3 폴더) 가 없습니다")
        tokenizer = os.environ.get("CHUNKER_TOKENIZER_PATH") or str(Path(model) / "tokenizer.json")
        print(f"새로 넣을 문서 {len(todo)}개 · KURE-v3 불러오는 중 (수십 초)")
        embedder, parser, storage = KureEmbedder(), DocParser(), LocalStorage.from_env()
        for i, (d, doc_id) in enumerate(todo, 1):
            args = SimpleNamespace(owner=d["owner"], title=d["title"], category=d["category"], scope=d["scope"],
                                   grade=int(d["grade"]), dept=None, doc_id=doc_id, many=False)
            start = time.monotonic()
            try:
                msg = await register(SEED_DIR / d["file"], args, parser=parser, embedder=embedder, storage=storage,
                                     tokenizer=tokenizer)
            except Exception as e:  # 하나가 실패해도 나머지는 넣는다
                msg = f"오류 {type(e).__name__}: {e}"
            print(f"  [{i}/{len(todo)}] {d['title']}: {msg} ({time.monotonic() - start:.0f}초)")
    async with session_scope() as s:
        n = await s.scalar(select(func.count(Doc.id)).where(Doc.status == "active", Doc.scope == "company"))
    print(f"끝. DB 의 전사 공개 문서 {n}개 (화면 왼쪽 '사내 문서' 에서 확인)")
    await dispose_engine()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-test", action="store_true", help="권한 시연용 부서 문서는 넣지 않음")
    args = ap.parse_args()
    load_dotenv()
    asyncio.run(main_async(include_test=not args.no_test))


if __name__ == "__main__":
    main()
