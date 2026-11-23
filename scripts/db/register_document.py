"""사내 문서 등록: 원본 저장 → 파싱 → 청킹 → KURE-v3 임베딩 → chunk 표 → 현재 버전으로 바꾸기.

    uv run python scripts/db/register_document.py 규정.hwpx --title "여신규정" --category 내규 \
        --owner admin --scope company --grade 1
    uv run python scripts/db/register_document.py 폴더/ --category 내규 --owner admin      # 폴더 안 파일 모두 (제목 = 파일 이름)
    uv run python scripts/db/register_document.py 새판.hwpx --doc-id 12 --owner admin     # 이미 있는 문서의 새 버전

- --scope dept 면 --dept(읽을 수 있는 부서)를 주거나, 주지 않으면 --owner 의 부서가 된다.
- 같은 문서에 같은 파일(sha256)을 다시 올리면 건너뛴다.
- 파싱에 실패하면 그 버전은 failed 로 남고 현재 버전은 바뀌지 않는다.
- 필요한 값(.env): DATABASE_URL, EMBEDDING_MODEL_PATH(KURE-v3 폴더), CHUNKER_TOKENIZER_PATH(없으면 모델 폴더의 tokenizer.json),
  STORAGE_ROOT(원본 저장 폴더), PARSER_MODELS_DIR(PDF·이미지 파싱 모델, HWP·HWPX 만이면 없어도 됨)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "apps/api/src")]

from sqlalchemy import func, select, text, update  # noqa: E402

from scripts.db._env import load_dotenv  # noqa: E402

CATEGORIES = ("내규", "매뉴얼", "상품", "법령", "공문")
SUFFIXES = {".hwp", ".hwpx", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".txt", ".md"}
MIME = {".hwp": "application/x-hwp", ".hwpx": "application/hwp+zip"}


def _files(paths: list[Path]) -> list[Path]:
    out = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUFFIXES)
        else:
            out.append(p)
    return out


async def register(path: Path, args, *, parser, embedder, storage, tokenizer: str | None) -> str:
    from db.engine import session_scope
    from db.models import Document, DocumentVersion, User
    from knowledge.ingestion.indexer import index_document_version

    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    async with session_scope() as s:
        owner = (await s.execute(select(User).where(User.login_id == args.owner))).scalar_one_or_none()
        if owner is None:
            raise SystemExit(f"사용자 {args.owner!r} 없음 — 먼저 scripts/db/seed_users.py 를 돌리세요")
        if args.doc_id:
            doc = await s.get(Document, args.doc_id)
            if doc is None:
                raise SystemExit(f"document {args.doc_id} 없음")
        else:
            doc = Document(owner_user_id=owner.id, owner_dept_id=args.dept or owner.dept_id,
                           title=args.title if (args.title and not args.many) else path.stem,
                           category=args.category, scope=args.scope, security_grade=args.grade)
            s.add(doc)
            await s.flush()
        dup = await s.scalar(select(DocumentVersion.id).where(DocumentVersion.doc_id == doc.id, DocumentVersion.sha256 == sha))
        if dup:
            await s.rollback()
            return f"건너뜀 (같은 파일이 이미 버전 {dup})"
        ver = (await s.scalar(select(func.max(DocumentVersion.version)).where(DocumentVersion.doc_id == doc.id)) or 0) + 1
        key = f"documents/{doc.id}/v{ver}/{path.name}"
        stored = storage.save(key, data, overwrite=True)
        mime = MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        dv = DocumentVersion(doc_id=doc.id, version=ver, is_current=False, file_name=path.name, mime_type=mime,
                             storage_path=stored.key, sha256=stored.sha256)
        s.add(dv)
        await s.flush()

        local = storage.local_path(stored.key)
        result = await asyncio.to_thread(parser.parse, local, path.name)
        parsed_file = result.save_beside(local, dv.id, include_docling=False)
        dv.parsed_path = Path(parsed_file).relative_to(storage.root).as_posix()
        parsed = result.to_dict(dv.id, include_docling=False)
        n = await index_document_version(s, dv.id, parsed, embedder=embedder, tokenizer_path=tokenizer)
        if dv.status == "ready":
            await s.execute(update(DocumentVersion).where(DocumentVersion.doc_id == doc.id, DocumentVersion.id != dv.id)
                            .values(is_current=False))
            await s.flush()
            dv.is_current = True
        await s.commit()
        state = "현재 버전" if dv.status == "ready" else f"실패: {result.reason}"
        return f"document {doc.id} v{ver} (document_version {dv.id}) 청크 {n}개 · {state}"


async def main_async(args) -> None:
    from db.engine import dispose_engine
    from knowledge.ingestion.embedders import KureEmbedder
    from knowledge.ingestion.parsers.pipeline import DocumentParser
    from storage.local import LocalStorage

    files = _files(args.paths)
    if not files:
        raise SystemExit("등록할 파일이 없습니다")
    args.many = len(files) > 1
    model = os.environ.get("EMBEDDING_MODEL_PATH")
    tokenizer = os.environ.get("CHUNKER_TOKENIZER_PATH") or (str(Path(model) / "tokenizer.json") if model else None)
    print(f"파일 {len(files)}개 · KURE-v3 불러오는 중 (CPU, 수십 초)")
    embedder = KureEmbedder()
    parser = DocumentParser()
    storage = LocalStorage.from_env()
    for f in files:
        try:
            print(f"{f.name}: {await register(f, args, parser=parser, embedder=embedder, storage=storage, tokenizer=tokenizer)}")
        except SystemExit:
            raise
        except Exception as e:
            print(f"{f.name}: 오류 {type(e).__name__}: {e}")
    await dispose_engine()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path, help="파일 또는 폴더")
    ap.add_argument("--owner", required=True, help="등록하는 사람 login_id (users 표)")
    ap.add_argument("--title", help="문서 제목 (파일 하나일 때만, 없으면 파일 이름)")
    ap.add_argument("--category", choices=CATEGORIES, default="내규")
    ap.add_argument("--scope", choices=("company", "dept"), default="company")
    ap.add_argument("--dept", type=int, help="scope=dept 일 때 읽을 수 있는 부서 (기본: 등록자 부서)")
    ap.add_argument("--grade", type=int, default=1, choices=range(1, 6), help="보안등급 1~5")
    ap.add_argument("--doc-id", type=int, help="이미 있는 문서에 새 버전으로 올릴 때")
    args = ap.parse_args()
    load_dotenv()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
