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
| 7. 문서 넣기 (`seed_docs.py` 한 줄) | Windows (저장소 맨 위) | 한 번 |

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
EMBEDDING_MODEL_PATH="C:/models/KURE-v3"
```

- `STORE=db`: 에이전트·공유 요청·대화 기록을 DB 에 저장 (껐다 켜도 남고, 왼쪽 목록에서 지난 대화를 다시 연다)
- `DOC_STORE=db`: 문서 검색을 DB(pgvector) 에서 한다. 7단계에서 문서를 넣어야 검색 결과가 나온다
- `EMBEDDING_MODEL_PATH`: 질문을 벡터로 바꾸는 실제 KURE-v3 폴더 (`config.json`, `tokenizer.json`, 모델 가중치 포함). 위 예시를 자기 PC 경로로 바꾼다. 경로에 빈칸이 있으면 큰따옴표로 감싸고 `/`를 사용한다. 폴더 경로 앞에 `KURE-v3` 같은 설명 문구를 덧붙이지 않는다. `uv run --env-file .env`의 형식 오류가 나면 DB 관련 설정도 적용되지 않을 수 있다.

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
uv sync --all-packages --extra hwp      # hwp = 한글 파일 읽기 (빼면 기업은행 문서가 안 들어가요)
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

문서는 **각자 DB 에만** 들어간다 (DB 내용과 `data/storage` 는 git 에 안 올라간다). 그래서 PC 마다 한 번 넣는다.
원본 파일은 저장소에 같이 들어 있다 (`data/seed_docs/`, 기업은행 공개 내규 10개 + 권한 시연 문서 1개). 명령 한 줄:

```powershell
uv run python scripts/db/seed_docs.py
```

- 기업은행 내규 10개: 전사 공개, 보안등급 1, 제목 "기업은행 ○○규정" (목록은 `data/seed_docs/manifest.yaml`)
- 벡터 DB 에는 전사 공개 문서(기업은행 내규 10개)만 넣는다. 예전 "개인채무자 FAQ (부서 10 전용 시험)" 은 11/24 에 뺐다 (이미 넣었다면 아래 "FAQ 삭제")
- 처음에는 약 10분 (조각 528개). 다시 돌려도 이미 있는 문서는 건너뛴다
- 확인: 화면 왼쪽 **사내 문서** 에 "전사 공개 문서 10개 · 검색 조각 528개"

**FAQ 삭제** (11/24 전에 7단계를 한 사람만, 한 번): 예전 목록의 시험 문서는 우리 기조(전사 문서만)와 맞지 않아 DB 에서 지운다.

```bash
# 1) 파일 위치 확인: 나온 경로(documents/번호/v1/…)의 '번호'를 메모. 아무것도 안 나오면 원래 없던 것이라 끝
sudo -u postgres psql -d agent_platform -tAc "SELECT v.storage_path FROM doc d JOIN doc_version v ON v.doc_id = d.id WHERE d.title = '개인채무자 FAQ (부서 10 전용 시험)'"
# 2) DB 에서 삭제: 문서 1건이 지워지면 버전·검색 조각도 같이 지워진다. DELETE 1 이면 끝
sudo -u postgres psql -d agent_platform -c "DELETE FROM doc WHERE title = '개인채무자 FAQ (부서 10 전용 시험)'"
```

3) 파일 폴더 삭제: 저장소의 `data\storage\documents\<1)에서 메모한 번호>` 폴더를 지운다 (`.env` 의 `STORAGE_ROOT` 를 바꿨다면 그 폴더 아래).
DB 행을 지운 뒤라 이 폴더는 아무 데서도 쓰지 않는다. 영구 삭제라 되돌리려면 파일부터 다시 등록해야 한다.

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
- 기업은행 내규 10개 등록, 다른 부서 직원(E2001)으로 검색해 내규 조항을 찾는 것 확인
- 다른 팀원 PC 에서는 아직 안 해 봄


## UC-01 규정 안내의 근거 검증

- 검색·원문 열람 위주의 커스텀 에이전트, 또는 규정/추심 등 규정 근거가 필요한 질문은 서비스가 규정 모드를 적용한다. 기존 에이전트도 재생성 없이 적용되며 DB 마이그레이션은 없다.
- 현재 사용자의 권한으로 검색 → 관련 후보 원문 열람 → 모델이 JSON으로 직접 인용 선택 → 서버가 원문 일치 확인 → 선택된 문단 재열람 → 인용/출처 출력 순서다.
- 사용자 질문, 개인 역할 지시, 이전 대화의 도구 결과는 이번 답의 규정 근거로 인정하지 않는다. 대화 기록은 삭제하지 않는다.
- 무관한 검색 결과, 근거 없음, 읽기 실패, 모델의 자유 설명/형식 오류, 인용 불일치는 “현재 접근 가능한 사내 문서에서 해당 질문의 근거를 찾지 못했습니다.”로 종료한다. 제한 없음·다른 규정 적용 가능성 등을 추측하지 않는다.
- 원문 열람 도구가 없는 경우에는 잘리지 않은 검색 미리보기만 근거로 사용할 수 있다. 긴 문단은 원문 열람 도구를 추가해야 한다.
- 검색 점수는 도구 응답의 `score`에 포함한다. `DOC_EVIDENCE_MIN_SCORE=0.30`은 이번 KURE-v3/시연 문서의 7개 질문으로 보정한 시작값이며, 새로운 문서를 넣으면 정상·없는 규정 질문을 함께 평가해 조정한다. 점수만으로 정답을 보장하지 않고 주제 및 원문 인용 검사를 함께 적용한다.
- 보수적으로 차단하므로 정상 질문도 검색 표현/모델의 인용 선택에 따라 근거 부족으로 안내될 수 있다. 현재는 자유로운 해석·요약보다 검증된 원문 인용을 우선한다.
- 문서 자체와 문단의 공개 범위·부서·보안등급을 모두 SQL에서 검사한다. 없는 문단과 접근 불가 문단은 같은 안내를 사용한다.
- 코드 변경 후 FastAPI를 재시작하고 새 대화로 테스트한다. 예전에 저장된 잘못된 답변을 소급 수정하지는 않는다.

회귀 테스트: `uv run --no-sync python -m pytest tests/test_regulation_evidence.py tests/test_lookup_rules.py tests/test_grounding.py -q`
