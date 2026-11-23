# DB 마이그레이션 (Alembic)

PostgreSQL 16 + pgvector 표 구조 변경 이력. 저장소 맨 위에서 실행한다.

`.env` 에 넣을 값:

```
DATABASE_URL=postgresql+asyncpg://postgres:비밀번호@127.0.0.1:5432/agent_platform
STORAGE_ROOT=          # 원본 파일 저장 폴더. 비우면 data/storage
```

| 파일 | 내용 |
|---|---|
| `versions/0001_team_erd.py` | 팀 ERD 그대로 (`docs/architecture/schema.postgres.sql`, 24개 표). 손으로 고치지 않는다 |
| `versions/0002_db_hardening.py` | SSO 연결 칸, 보안등급 1~5, 감사 로그 추가만 허용, 앱 전용 역할 `agent_app` |
| `versions/0003_rename_document_id.py` | `document_id` → `doc_id` (document_version·agent_knowledge), `document_version_id` → `doc_version_id` (chunk·output_source) |
| `versions/0004_chunk_embedding_halfvec.py` | **[초안, DB 담당 확인 전]** `chunk.embedding` vector(1024) → halfvec(2048) (KURE-v3). 기존 벡터는 비워지므로 색인을 다시 돌린다 |
| `versions/0005_rename_department_to_dept.py` | 테이블 `department` → `dept` (제약·시퀀스 이름 포함) |
| `versions/0006_rename_document_to_doc.py` | 테이블 `document` → `doc`, `document_version` → `doc_version` (제약·인덱스·시퀀스 이름 포함), `audit_log.target_type` 에 `'doc'` 추가 |

## 자주 쓰는 명령

```powershell
uv run alembic upgrade head          # 최신 구조로
uv run alembic current               # 지금 DB 가 어느 단계인지
uv run alembic downgrade -1          # 한 단계 되돌리기
uv run alembic upgrade head --sql    # DB 에 붙지 않고 실행될 SQL 만 보기
```

## 구조를 바꿀 때 (규칙)

1. 변경 파일은 **DB 담당만** 만든다. 여러 사람이 동시에 만들면 순서가 갈라진다.
2. `uv run alembic revision --rev-id 0003 -m "무엇을 바꿨나"` 로 빈 파일을 만들고 `upgrade` · `downgrade` 를 채운다.
3. 같은 변경을 `apps/api/src/db/models.py` 에도 반영하고, `tests/db` 의 모델·DB 일치 테스트로 확인한다.
4. 트리거 · CHECK · 부분 인덱스는 자동 생성(`--autogenerate`)이 못 잡으니 직접 쓴다.
5. 팀 ERD(`docs/architecture/schema.postgres.sql`)를 바꾼 경우 그 차이만 새 변경 파일로 옮긴다 (0001 은 고치지 않음).

## 앱 로그인 계정

0002 가 만드는 `agent_app` 은 로그인할 수 없는 권한 묶음이다. 앱이 쓸 계정은 DB 관리자가 직접 만든다 (비밀번호를 git 에 남기지 않으려고 마이그레이션에 넣지 않음).

```sql
CREATE ROLE agent_platform_app LOGIN PASSWORD '...' IN ROLE agent_app;
```

이 계정으로 붙으면 `audit_log` 는 읽기·추가만 된다. 로컬 개발에서는 `postgres` 계정을 그대로 써도 되지만, 그러면 권한 분리는 적용되지 않는다 (트리거 보호는 그대로 적용).
