"""[초안 — DB 담당 확인 전] chunk.embedding: vector(1024) → halfvec(2048) (2026-11-20, 청킹 담당 제안)

이유
- 임베딩 모델이 KURE-v3 로 정해졌다(2,048차원). 0001 의 vector(1024) 는 이전 후보 bge-m3 기준.
- pgvector HNSW 인덱스는 vector 2,000차원까지, halfvec(16비트) 4,000차원까지라서 2,048차원은 halfvec 로 둔다.
- halfvec(2048) 한 개 = 4KB 로 지금 vector(1024) 와 저장 크기가 같다.
- 2차 청킹 실험(내규 문서 17개, 질문 108개): halfvec(2048) 흉내는 2048(float32)과 검색 결과가 완전히 같음
  (Recall@1 92.6%, Recall@5 100%, MRR 0.957). 1024로 자르면 Recall@1 90.7%, MRR 0.947.

주의
- 차원이 달라 기존 벡터는 옮길 수 없다. 칸을 비우고(NULL) 색인을 다시 돌려야 한다
  (indexer.index_document_version 을 문서 버전마다 다시 부르면 됨). 되돌릴 때도 마찬가지.
- 코드: db/models.py 의 EMBEDDING_DIM·칸 종류, KureEmbedder(dim=2048).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunk_embedding")
    op.execute("ALTER TABLE chunk ALTER COLUMN embedding TYPE halfvec(2048) USING NULL::halfvec(2048)")
    op.execute("CREATE INDEX idx_chunk_embedding ON chunk USING hnsw (embedding halfvec_cosine_ops)")
    op.execute("COMMENT ON COLUMN chunk.embedding IS 'KURE-v3 임베딩 (halfvec 2048차원)'")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunk_embedding")
    op.execute("ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(1024) USING NULL::vector(1024)")
    op.execute("CREATE INDEX idx_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops)")
    op.execute("COMMENT ON COLUMN chunk.embedding IS 'bge-m3 임베딩 (1024차원)'")
