# -*- coding: utf-8 -*-
"""구조 기반 청킹: 파서 결과(parsed/{id}.json) → 청크 목록 → 원본 옆 chunks/{doc_version_id}.jsonl.

    from knowledge.ingestion.chunkers import chunk_doc, save_chunks_beside
    chunks = chunk_doc(parsed_dict, tokenizer_path="models/KURE-v3/tokenizer.json")
    save_chunks_beside("원본.hwpx", 123, chunks)

설계와 근거: 청킹 인수인계(설계 결정 13개) + 2차 청킹 실험(구조 기반 청커 C2 채택, 상한 512토큰).
청크 필드: 팀 DB chunk 표(db/models.py Chunk)와 같은 이름 — doc_version_id, chunk_index, page, content(원문, 표는 마크다운),
owner_dept_id·scope·security_grade(권한, 문서에서 물려받음), heading_path.
DB 표에 없는 것(jsonl·디버깅용): embed_text(임베딩에 넣을 글 = 제목 경로 + 본문, 표는 "열이름: 값"), kind(본문/목록/섞임/표),
pages, table_part("1/3"), source_spans(파서 항목 번호와 공백 뺀 글 위치: 원문 하이라이트·감사용), header_rows_of.
"""
import json
from pathlib import Path

from .doc import Doc
from .structure import MIN_TOKENS, _COUNT, chunk_structure
from .tokens import TOKENIZER_ENV, token_counter

CHUNKER_VERSION = "structure-c2/1.0"
DEFAULT_MAX_TOKENS = 512            # 2차 청킹 실험: 512 vs 1024 차이 없음(구조·검색) → 512
PERMISSION_KEYS = ("owner_dept_id", "scope", "security_grade")     # 팀 DB chunk 표 칸 이름

__all__ = ["chunk_doc", "save_chunks_beside", "token_counter", "CHUNKER_VERSION", "DEFAULT_MAX_TOKENS", "TOKENIZER_ENV"]


def chunk_doc(parsed: dict, *, max_tokens: int = DEFAULT_MAX_TOKENS, min_tokens: int = MIN_TOKENS,
                   count_tokens=None, tokenizer_path=None, doc_meta: dict | None = None) -> list[dict]:
    """파서 결과 dict → 청크 목록. status가 ok가 아니면 빈 목록.

    count_tokens: 토큰 수를 세는 함수(없으면 tokenizer_path 또는 환경변수 CHUNKER_TOKENIZER_PATH의 토크나이저).
    doc_meta: 문서(doc 표)에서 물려받을 권한 {"owner_dept_id", "scope", "security_grade"}.
    """
    if parsed.get("status") != "ok":
        return []
    count = count_tokens or token_counter(tokenizer_path)
    doc = Doc(parsed)
    token = _COUNT.set(count)
    try:
        chunks = chunk_structure(doc, max_tokens, min_tokens=min_tokens)
    finally:
        _COUNT.reset(token)
    meta = doc_meta or {}
    for c in chunks:
        c["doc_version_id"] = parsed.get("doc_version_id")
        for k in PERMISSION_KEYS:
            c[k] = meta.get(k)
        c["chunker_version"] = CHUNKER_VERSION
        c["max_tokens"] = max_tokens
    return chunks


def save_chunks_beside(original_path, doc_version_id, chunks: list[dict]) -> Path:
    """원본 옆 chunks/{doc_version_id}.jsonl (한 줄에 청크 하나, 파서의 parsed/ 저장과 같은 원칙)."""
    out = Path(original_path).parent / "chunks" / f"{doc_version_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return out
