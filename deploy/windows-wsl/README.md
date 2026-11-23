# 내 PC 를 서버로 만들기 (Windows + WSL, 개발·시연용)

팀원 PC 는 서로 같은 망에 묶여 있지 않으므로 **각자 자기 PC 에 DB 를 만든다**.
표 구조(alembic)·사용자(seed 스크립트)는 git 으로 같이 쓰니, 아래 순서를 따르면 누구 PC 든 같은 DB 가 된다.
시연 날에는 시연 PC 한 대에서 이 순서대로 준비하고, 그 PC 의 브라우저로 계정을 바꿔 가며 보여 준다.

```
[Windows]  코드(이 저장소) · uv · 앱(FastAPI 8000) · 화면(Vite 5173) · 브라우저
   │  127.0.0.1:5432 (WSL 이 Windows 로 자동 연결)
[WSL 우분투]  PostgreSQL 16 + pgvector
```

| 단계 | 어디서 | 한 번만? |
|---|---|---|
| 1. WSL 우분투 | Windows PowerShell(관리자) | 한 번 |
| 2. PostgreSQL 16 + pgvector | WSL 우분투 | 한 번 |
| 3. DB 와 확장 만들기 | WSL 우분투 | 한 번 |
| 4. `.env` | Windows (저장소 맨 위) | 한 번 |
| 5. 표 · 사용자 · 비밀번호 | Windows (저장소 맨 위) | 표가 바뀔 때마다 `alembic upgrade head` |
| 6. 켜기 · 로그인 | Windows | 매번 |
| 7. 문서 넣기 (기업은행 내규 10개) | Windows (저장소 맨 위) | 한 번 |

---

## 1. WSL 우분투 (이미 있으면 건너뜀)

```powershell
wsl -l -v                      # Ubuntu 가 보이면 2단계로
wsl --install -d Ubuntu        # 없으면 설치 후 재부팅, 우분투 사용자 이름·비밀번호 만들기
```

## 2. PostgreSQL 16 + pgvector (WSL 우분투 안에서)

PostgreSQL 공식 저장소(PGDG)를 등록하고 설치한다. pgvector 는 **0.8 이상** 이어야 한다.

```bash
sudo apt update && sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh     # 공식 저장소 등록 (Enter)
sudo apt install -y postgresql-16 postgresql-16-pgvector
psql --version                                                  # 16.x
sudo service postgresql start                                   # 꺼져 있으면 켜기
```

## 3. DB 와 확장 만들기 (WSL 우분투 안에서)

```bash
sudo -u postgres psql -c "\password postgres"                   # DB 관리자 비밀번호 정하기 (4단계에서 씀)
sudo -u postgres createdb agent_platform
sudo -u postgres psql -d agent_platform -c "CREATE EXTENSION IF NOT EXISTS vector"
sudo -u postgres psql -d agent_platform -tAc "SELECT extversion FROM pg_extension WHERE extname='vector'"   # 0.8 이상
```

## 4. `.env` (Windows, 저장소 맨 위)

```powershell
copy .env.example .env
```

`.env` 에서 아래 다섯 줄을 고친다 (`.env` 는 git 에 안 올라간다).

```
DATABASE_URL=postgresql+asyncpg://postgres:3단계에서_정한_비밀번호@127.0.0.1:5432/agent_platform
AUTH_MODE=session
STORE=db
DOC_STORE=db
EMBEDDING_MODEL_PATH=내_PC의_KURE-v3_폴더_경로
```

- `STORE=db`: 에이전트·공유 요청·대화 기록을 DB 에 저장 (껐다 켜도 남고, 왼쪽 목록에서 지난 대화를 다시 연다)
- `DOC_STORE=db`: 문서 검색을 DB(pgvector) 에서 한다. 7단계에서 문서를 넣어야 검색 결과가 나온다
- `EMBEDDING_MODEL_PATH`: 질문을 벡터로 바꾸는 KURE-v3 폴더 (git 에 없음, 각자 가진 폴더 경로. 경로에 빈칸이 있어도 따옴표 없이)

LLM(Ollama) 은 팀 기준 모델을 쓴다. 채팅은 `qwen3:8b-24k`, 에이전트 초안 만들기는 `qwen3:14b`.
`LLM_MODEL`·`DRAFT_LLM_MODEL` 은 `.env.example` 그대로 두고, 모델은 한 번 받는다:

```powershell
ollama pull qwen3:8b
ollama create qwen3:8b-24k -f config/ollama/qwen3-8b-24k.Modelfile
ollama pull qwen3:14b                              # 약 9.3GB. 안 받으면 초안도 qwen3:8b-24k 로 만든다
```

`LLM_MODEL=qwen3:8b` 로 바꾸면 기억 한도가 4,096 토큰이라 바로 앞 대화도 잘려서 "방금 말한 것" 을 모른다 (config/ollama/README.md).

## 5. 표 · 사용자 · 비밀번호 (Windows, 저장소 맨 위)

```powershell
uv sync
uv run alembic upgrade head                        # 표 만들기 (지금 0010). 누가 새 마이그레이션을 올리면 다시 실행
uv run python scripts/db/seed_users.py             # 부서·직원 (config/dev_users.yaml 의 사번 E1001 · E1002 · E2001 · E2002 · E0001)
uv run python scripts/db/set_password.py --all     # 모두에게 같은 비밀번호 (두 번 입력, 8자 이상. 시연은 qwer1234)
```

한 사람만 바꾸려면 `uv run python scripts/db/set_password.py E1001`. 바꾸면 그 사람의 기존 로그인은 끊긴다.

## 6. 켜기 · 로그인 (Windows, 매번)

터미널 두 개:

```powershell
uv run python -m uvicorn main:app --app-dir apps/api/src --env-file .env      # 백엔드 (8000)
```

```powershell
cd apps/web; npm install; npm run dev                                         # 화면 (5173), npm install 은 처음 한 번
```

브라우저에서 `http://localhost:5173` → 로그인 화면 → 아이디는 **사번** (예: `E1001`) / 5단계 비밀번호.
계정 (사번은 Gmail 가상 메일함과 같다, 이름은 팀원): `E1001` 김도영 팀원(부서 10) · `E1002` 박종원 부서장(부서 10) · `E2001` 나치훈 팀원(부서 20) · `E2002` 손채영 부서장(부서 20) · `E0001` 이형민 관리자.
중간 발표까지는 사원 추가·가입 화면 없이 기존 직원이 모두 등록돼 있다고 가정한다 (2026-11-23 팀 합의).

확인:
- 틀린 비밀번호 → "사번 또는 비밀번호가 올바르지 않습니다." (5번 연속이면 10분 잠금)
- 새로고침해도 로그인 유지, 위쪽 **로그아웃** 누르면 로그인 화면
- 로그인 기록: `audit_log` 표의 `login` · `login_failed` · `logout`

로그인 없이 예전처럼 쓰려면 `.env` 의 `AUTH_MODE=dev` (왼쪽 아래 사용자 선택).

## 7. 문서 넣기 (Windows, 저장소 맨 위, 한 번)

문서는 **각자 DB 에만** 들어간다 (원본 저장 폴더 `data/storage` 도 git 에 안 올라간다). 그래서 PC 마다 한 번씩 넣는다.
UC-01(규정 질의응답) 이 쓰는 문서는 기업은행 공개 내규 10개다. 원본 hwp 10개(파일 이름 `여신규정_2025-12-30.hwp` 처럼 `이름_날짜`)를 한 폴더에 받아 두고:

```powershell
$dir = "기업은행_내규_10개를_받은_폴더"
Get-ChildItem $dir -Filter *.hwp | ForEach-Object {
  $name = $_.BaseName -replace '_\d{4}-\d{2}-\d{2}$', ''
  uv run python scripts/db/register_doc.py $_.FullName --title "기업은행 $name" --category 내규 --owner E0001 --scope company --grade 1
}
```

- 전사 공개(`company`), 보안등급 1, 등록자 관리자(`E0001`). 제목은 "기업은행 예금규정" 처럼 된다.
- 10개에 약 10분 (조각 528개). 같은 파일을 다시 넣으면 건너뛴다.
- 확인: `wsl -u postgres -e psql -d agent_platform -c "SELECT id, title FROM doc ORDER BY id"` 에 10줄

부서 공개 시험용 문서(선택): 부서 10 사람만 보이는 문서가 있어야 "다른 부서는 못 본다" 를 보여 줄 수 있다.

```powershell
uv run python scripts/db/register_doc.py 13_금융위_개인채무자FAQ.hwpx --title "개인채무자 FAQ (부서 10 전용 시험)" --category 공문 --owner E1002 --scope dept --grade 2
```

## 자주 막히는 곳

| 증상 | 해결 |
|---|---|
| 백엔드가 `DATABASE_URL 이 없습니다` | 실행 명령에 `--env-file .env` 를 빠뜨림 |
| 로그인이 갑자기 안 됨, 백엔드 로그에 `ConnectionRefusedError` (5432) | **WSL 이 저절로 꺼진 것.** 열린 WSL 창이 없으면 Windows 가 잠시 뒤 WSL 을 끄고 DB 도 같이 꺼진다. 시연·작업 중에는 터미널 하나에 `wsl -e sleep infinity` 를 켜 두거나 우분투 창을 열어 둔다. 켜도 안 되면 WSL 에서 `sudo service postgresql start` |
| 이어서 물으면 앞 대화를 모름, 로그에 `컨텍스트 한도(4096 토큰)` | `.env` 가 `LLM_MODEL=qwen3:8b`. 4단계대로 `qwen3:8b-24k` 를 만들고 그 이름으로 |
| `password authentication failed` | `.env` 비밀번호가 3단계와 다름 |
| `relation "login_session" does not exist` | `uv run alembic upgrade head` 를 안 함 |
| 로그인 화면이 안 나오고 사용자 선택이 보임 | `.env` 에 `AUTH_MODE=session` 이 없음 (백엔드 다시 켜기) |

## 확인 상태 (2026-11-23)

- 이형민 PC: WSL 우분투 + PostgreSQL 16.16 + pgvector 0.8.7 에서 0010 까지 적용, 테스트 492개 통과
  (Windows 에서만 `tests/test_api.py::test_approval_flow_through_api` 2개 실패: 감사 파일을 읽을 때 인코딩을 안 줘서 한글이 cp949 로 읽힘. 이번 작업 전부터 있던 것),
  브라우저로 틀린 비밀번호 거절 → 로그인 → 새로고침 유지 → 로그아웃, 지난 대화 다시 열기까지 확인
- 기업은행 내규 10개 + 부서 10 시험 문서 1개 등록, 다른 부서 직원(E2001)으로 검색해 내규 조항을 찾는 것 확인
- 다른 팀원 PC 에서는 아직 안 해 봄
