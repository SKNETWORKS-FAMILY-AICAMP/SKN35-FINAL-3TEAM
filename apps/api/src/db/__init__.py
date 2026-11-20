"""플랫폼 DB (PostgreSQL 16 + pgvector).

  engine.py   연결 (DATABASE_URL, asyncpg) · 세션 · FastAPI 의존성
  models.py   ORM 모델 — docs/architecture/schema.postgres.sql 과 같은 표·칸
  표 구조를 바꾸는 일은 저장소 맨 위 alembic/ 의 마이그레이션으로만 한다 (모델만 고치면 DB 는 안 바뀐다).
"""
