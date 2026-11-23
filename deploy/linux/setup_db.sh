#!/usr/bin/env bash
# DB 만들기: 데이터베이스 → pgvector 확장(0.8 이상 확인) → 표(alembic 0001~) → 앱 계정
#   sudo -u postgres bash deploy/linux/setup_db.sh [DB이름] [앱계정]
# 앱 계정 비밀번호는 실행 중에 입력받는다 (명령줄·파일에 남기지 않음).
# alembic 은 .env 의 DATABASE_URL 로 붙는다 (DB 관리자 계정 주소를 넣어 두고 돌린 뒤, 앱 계정 주소로 바꾼다).
set -euo pipefail

DB="${1:-agent_platform}"
APP_USER="${2:-agent_platform_app}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"

echo "1) 데이터베이스 $DB"
if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB'" | grep -q 1; then
  createdb "$DB"
fi

echo "2) pgvector 확장"
psql -d "$DB" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector"
VER="$(psql -d "$DB" -tAc "SELECT extversion FROM pg_extension WHERE extname = 'vector'")"
python3 - "$VER" <<'PY'
import sys
v = tuple(int(x) for x in sys.argv[1].split(".")[:2])
if v < (0, 8):
    sys.exit(f"pgvector {sys.argv[1]} — 0.8 이상이 필요합니다 (halfvec, hnsw.iterative_scan)")
print(f"   pgvector {sys.argv[1]} OK")
PY

echo "3) 표 만들기 (alembic upgrade head)"
( cd "$ROOT" && "$PY" -m alembic upgrade head < /dev/null )   # 표준 입력을 막아 아래 비밀번호 입력을 먹지 않게

echo "4) 앱 계정 $APP_USER (권한 묶음 agent_app 소속)"
if ! psql -d "$DB" -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$APP_USER'" | grep -q 1; then
  read -r -s -p "   $APP_USER 비밀번호: " PW; echo
  # psql 변수(:'pw')는 -c 에서는 바뀌지 않으므로 표준 입력으로 넘긴다 (값을 따옴표로 안전하게 감쌈)
  psql -d "$DB" -v ON_ERROR_STOP=1 -v pw="$PW" -v u="$APP_USER" <<'SQL'
CREATE ROLE :"u" LOGIN PASSWORD :'pw' IN ROLE agent_app;
SQL
  unset PW
fi
echo "끝. .env 의 DATABASE_URL 을 앱 계정($APP_USER) 주소로 바꾸세요."
