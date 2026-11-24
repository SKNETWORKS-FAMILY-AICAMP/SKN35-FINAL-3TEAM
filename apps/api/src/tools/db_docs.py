"""사내 문서 검색·원문 열람을 팀 DB(PostgreSQL + pgvector)에서 하는 도구.

파일 예시(data/fixtures/doc_chunks.json)를 읽는 SearchDocsTool·ReadDocTool 과
이름(search_doc, get_doc_chunk)·설명·입력·결과 모양이 같다. 그래서 하네스·근거 확인 코드는 그대로 쓴다.

- 검색: 질문 → KURE-v3 질문 벡터 → 볼 수 있는 청크만 뜻이 가까운 순 (knowledge.retrieval)
- 권한: 하네스 can_read_chunk 와 같은 규칙을 SQL 에서 먼저 건다
  (보안등급 사용자 ≥ 청크, scope 'company' 는 누구나, 'dept' 는 같은 dept_id 만)
- 켜기: 환경변수 DOC_STORE=db (services/container.py). 끄면 지금처럼 파일 예시를 읽는다.
"""
from __future__ import annotations

import asyncio
import json
import threading

from contracts.harness import ToolResult
from contracts.permission import ToolCall
from tools.read_doc import READ_DOC_SPEC
from tools.search_doc import PREVIEW_LENGTH, SEARCH_DOC_SPEC

DEFAULT_TOP_K = 5  # 검색 실험: 정답이 99% 이상 들어오는 가장 작은 k


class BackgroundEmbedder:
    """KURE-v3 를 서버가 뜰 때 다른 스레드에서 미리 올린다 (CPU 로딩 약 24초 → 첫 검색이 툴 시간 제한에 걸리지 않게).

    factory 는 실제 임베더를 만드는 함수 (예: lambda: KureEmbedder()). embed_query 는 로딩이 끝날 때까지 기다린다.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._ready = threading.Event()
        self._embedder = None
        self._error: BaseException | None = None
        threading.Thread(target=self._load, name="embedder-load", daemon=True).start()

    def _load(self) -> None:
        try:
            self._embedder = self._factory()
        except BaseException as e:  # 로딩 실패는 검색 때 알린다
            self._error = e
        finally:
            self._ready.set()

    def embed_query(self, text: str) -> list[float]:
        self._ready.wait()
        if self._error is not None:
            raise RuntimeError(f"임베딩 모델을 불러오지 못했습니다: {self._error}")
        return self._embedder.embed_query(text)


def _location(row: dict) -> str:
    """하네스 결과의 location: 문서 안 위치 ("제2장 … > 제12조 …"), 없으면 쪽, 그것도 없으면 몇 번째 문단."""
    # 파서가 FAQ 본문 전체를 제목으로 오인한 경우, 이를 조항/위치처럼 인용하지 않는다.
    path = [p for p in row.get("heading_path") or []
            if isinstance(p, str) and p and len(p) <= 100 and "\n" not in p and p != row.get("title")]
    if path:
        return " > ".join(path)
    if row.get("page"):
        return f"{row['page']}쪽"
    return f"{row['chunk_index'] + 1}번째 문단" if row.get("chunk_index") is not None else ""


def _session_factory():
    from db.engine import session_scope
    return session_scope()


class DbSearchDocsTool:
    spec = SEARCH_DOC_SPEC

    def __init__(self, embedder, *, session_factory=_session_factory, top_k: int = DEFAULT_TOP_K) -> None:
        self.embedder = embedder
        self.session_factory = session_factory
        self.top_k = top_k

    async def run(self, call: ToolCall) -> ToolResult:
        from knowledge.retrieval import principal_access, search_chunks

        query = call.arguments.get("query")
        if not isinstance(query, str):
            return ToolResult(call.call_id, False, "검색어를 문자열로 입력해 주세요.")
        query = query.strip()
        if not query:
            return ToolResult(call.call_id, False, "검색어를 입력해 주세요.")

        access = principal_access(call.principal)
        rows: list[dict] = []
        if access is not None:
            dept_id, grade = access
            try:
                qvec = await asyncio.to_thread(self.embedder.embed_query, query)
                async with self.session_factory() as session:
                    rows = await search_chunks(session, qvec, dept_id=dept_id, security_grade=grade, k=self.top_k)
            except Exception:
                return ToolResult(call.call_id, False, "문서 검색을 할 수 없습니다. 잠시 뒤 다시 시도해 주세요.")

        matches = [{
            "doc_id": str(r["doc_id"]),
            "chunk_id": str(r["chunk_id"]),
            "title": r["title"],
            "location": _location(r),
            "preview": r["content"][:PREVIEW_LENGTH],
            "is_truncated": len(r["content"]) > PREVIEW_LENGTH,
            "score": r["score"],
        } for r in rows]
        payload = {"query": query, "matches": matches, "search_backend": "db_vector"}
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)


class DbReadDocTool:
    spec = READ_DOC_SPEC

    def __init__(self, *, session_factory=_session_factory) -> None:
        self.session_factory = session_factory

    async def run(self, call: ToolCall) -> ToolResult:
        from knowledge.retrieval import get_chunk, principal_access

        chunk_id = call.arguments.get("chunk_id")
        if not isinstance(chunk_id, str):
            return ToolResult(call.call_id, False, "문단 ID를 문자열로 입력해 주세요.")
        chunk_id = chunk_id.strip()
        if not chunk_id:
            return ToolResult(call.call_id, False, "문단 ID를 입력해 주세요.")

        access = principal_access(call.principal)
        row = None
        if access is not None and chunk_id.isdigit():
            dept_id, grade = access
            try:
                async with self.session_factory() as session:
                    row = await get_chunk(session, int(chunk_id), dept_id=dept_id, security_grade=grade)
            except Exception:
                return ToolResult(call.call_id, False, "문서를 열 수 없습니다. 잠시 뒤 다시 시도해 주세요.")
        if row is None:  # 없는 문단과 권한 없는 문단을 같은 문구로 (존재 여부를 숨김)
            return ToolResult(call.call_id, False, "문단을 찾을 수 없습니다.")

        payload = {
            "doc_id": str(row["doc_id"]),
            "chunk_id": str(row["chunk_id"]),
            "title": row["title"],
            "location": _location(row),
            "text": row["content"],
        }
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
