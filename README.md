# Agent Argus — 사내 업무 에이전트 플랫폼 (SKN35 FINAL 3팀)

은행 직원이 자연어로 업무를 맡기면, 에이전트가 **허용된 도구만** 골라 실행하고 **확인한 근거로만** 답하는 사내 플랫폼입니다.
직원은 템플릿이나 자연어 설명으로 자기 에이전트를 만들고, 관리자는 공유·권한·감사 기록을 관리합니다.

- LLM 은 사내에서 돌리는 Ollama `qwen3:8b` (문서·대화를 외부 LLM API 로 보내지 않음)
- 도구 실행은 모두 **권한 검사(ABAC) → 필요하면 승인 카드 → 실행 → 감사 로그**를 지난다
- 규정·수치·고객 정보는 **조회 결과 없이 답하지 않는다** (답 검사·원문 인용 검증)

## 이번 범위의 기능

| 기능 | 무엇을 하나 | 쓰는 도구 |
|---|---|---|
| **규정 안내** (UC-01) | 사내 규정을 찾아 **원문 인용**으로만 답한다. 근거가 없으면 "찾지 못했습니다" | `search_doc` · `get_doc_chunk` |
| **금리·환율 리포트** (UC-02) | 한국은행 ECOS 의 환율·기준금리로 양식에 맞춘 아침 리포트를 만들어 내 문서에 저장 (PDF·DOCX) | 시장 MCP `market_snapshot` · 양식 검색/읽기 · `save_morning_report` |
| **만기 대출 정리표** (UC-03) | 내 담당 대출 중 만기가 가까운 건을 정리표 문서로 | `fill_document` (`maturity-report` 양식) |
| **첨부 문서 질의** | 올린 파일(HWP·HWPX·PDF·DOCX·XLSX 등)을 읽고 답한다. 사내 규정과 비교도 가능 | `parse_doc` (+ `search_doc`) |
| **메일** | 내 메일 검색·읽기, 초안 작성, 직원에게 발송 (승인 후) | Gmail MCP `search_mail` · `read_mail` · `send_mail` |

그 밖에 에이전트 만들기(자연어 → 초안)·공유 요청/승인, 알림함, 예약 실행(평일 아침 리포트·만기 알림), 내 문서가 있습니다.
심사 요청(UC-04)은 이번 범위에서 뺐습니다.

## 하네스: 플래너 → 단계마다 도구 하나 → 답 검사

채팅 한 번이 처리되는 순서입니다 (`apps/api/src/harness/`).

```
직원 요청
  │
  ▼
① 플래너 (planner.py)      LLM 이 에이전트의 도구 목록을 보고 계획(JSON)을 쓴다
  │                         {"steps": [{"tool": "search_doc", "goal": "..."}, ...]}
  ▼
② 계획 검사 (planner.py)   코드가 계획을 고친다. LLM 계획을 그대로 믿지 않는다
  │                         - 없는 도구·겹치는 단계·요청하지 않은 저장/발송 단계 빼기
  │                         - 조회 규칙(grounding.py)이 필요하다고 보는 조회 넣기
  │                         - 앞 단계가 필요한 도구는 그 단계를 앞에 (REQUIRES)
  │                         - 확인받고 보낼 것은 다음 요청으로 미루기 (CONFIRM_FIRST)
  ▼
③ 단계 실행 (plan_runner.py · steps.py)
  │                         단계마다 그 도구 하나만 LLM 에게 붙여 인자를 받는다.
  │                         코드가 정할 수 있는 인자는 코드가 정한다 (BIND):
  │                         기준일·양식·snapshot_id·첨부 file_id·메일 받는 사람 …
  │                         실행 뒤 처리 (AFTER): 0건이면 재검색, 리포트 본문 그대로 보여 주기 …
  │                         실행은 권한 검사 → 승인 → 실행 → 감사 로그 (runner._handle_tool_call)
  ▼
④ 답 쓰기 + 답 검사         - 일반 답: 숫자·근거·"보냈습니다" 같은 완료 주장을 이번 조회 결과와 대조 (answer_check.py)
                            - 규정 질문: LLM 은 인용할 원문만 고르고, 서버가 원문과 같은지 다시 확인 (regulation.py)
                            - 첨부 답: 원문 근거 검사, 걸리면 남은 부분을 이어 읽고 한 번 다시 쓰기 (attachment_grounding.py)
```

- `HARNESS_PLANNER=1`(기본)이면 이 흐름, `0`이면 예전 루프(LLM 이 도구를 고르는 루프)로 돈다.
- 계획은 서버 콘솔에 `계획 (llm, run …): search_doc(planner) → get_doc_chunk(rule)` 처럼 찍힌다.
- 플래너가 `HARNESS_PLANNER_TIMEOUT_SECONDS`(기본 60초) 안에 답하지 않으면 규칙만으로 계획한다.
- 권한 거부가 나면 남은 단계는 하지 않고 서버가 정한 문구로 끝낸다 (LLM 이 결과를 지어내지 않게).

**기능을 고치거나 더할 때** 손대는 곳

| 하고 싶은 것 | 파일 |
|---|---|
| 계획에 꼭 들어가야 할 단계·순서·뺄 단계 | `harness/planner.py` (`check_plan`, `REQUIRES`, `CONFIRM_FIRST`) |
| 특정 도구의 인자를 코드가 정하기 / 실행 뒤 처리 | `harness/steps.py` (`BIND`, `AFTER`) |
| "이런 질문은 이 조회 없이 답하면 안 됨" | `harness/grounding.py` (`DEFAULT_RULES`) |
| 답을 쓰는 방식 | `harness/plan_runner.py` |
| 도구 실행 권한·승인 | `config/permissions.yaml` |
| 새 도구 | `apps/api/src/tools/` + `ToolSpec.effect`(read·write·send) 지정 |

## 폴더 구조

```
apps/
  api/src/            FastAPI 백엔드
    harness/          에이전트 하네스 (위 설명)
    tools/            도구 (문서 검색·첨부 읽기·대출·시장·문서 만들기·Gmail …)
    permission/       ABAC 권한 검사 · 승인 · 감사 로그
    knowledge/        문서 파싱·청킹·임베딩(KURE-v3)·벡터 검색(pgvector)
    services/ routers/ db/
  web/                React + Vite 화면
contracts/            하네스·권한 계약 (팀 공통 인터페이스)
mcp-servers/
  market/             시장 데이터 MCP (8002) — 한국은행 ECOS, 기록 재생(replay) 모드
  gmail/              Gmail MCP (8001) — 직원 개인 계정, 가상 메일함(fake) 모드
config/               권한 정책 · 에이전트 템플릿 · 보고서/문서 양식 · 예약 작업 · Ollama 모델
alembic/              DB 마이그레이션
scripts/db/           사용자·비밀번호·사내 문서 넣기
data/                 시연 문서(seed_docs) · 고정 데이터(fixtures) · 로컬 저장소
deploy/               설치 안내 (Windows+WSL 개발·시연용, 리눅스 폐쇄망)
docs/                 기획서 · 요구사항 · ERD · 결정 사항(adr)
tests/                pytest (하네스·도구·권한·DB 등)
```

## 실행 (개발·시연 PC)

필요한 것: Python 3.12 + [uv](https://docs.astral.sh/uv/), Node.js 20.19+, PostgreSQL 16 + pgvector 0.8+, [Ollama](https://ollama.com/),
임베딩 모델 KURE-v3 폴더(팀 공유 폴더).

### 1. 처음 한 번

```bash
uv sync --all-packages --extra hwp          # 백엔드 + Gmail·시장 MCP 의존성
cp .env.example .env                        # DATABASE_URL, EMBEDDING_MODEL_PATH 등을 채운다 (아래 "설정")
ollama pull qwen3:8b
ollama create qwen3:8b-24k -f config/ollama/qwen3-8b-24k.Modelfile   # 긴 문서용 (config/ollama/README.md)
(cd apps/web && npm ci)
```

DB 는 [deploy/windows-wsl/README.md](deploy/windows-wsl/README.md) 순서대로 만듭니다 (PostgreSQL·pgvector 설치 → DB 만들기 →
`alembic upgrade head` → 사용자·비밀번호 → 사내 문서 넣기). 리눅스 서버 설치는 [deploy/linux/README.md](deploy/linux/README.md).

```bash
uv run alembic upgrade head
uv run python scripts/db/seed_users.py      # 부서·직원 (config/dev_users.yaml)
uv run python scripts/db/set_password.py --all
uv run python scripts/db/seed_docs.py       # 기업은행 공개 내규 10개 등 (data/seed_docs/)
```

### 2. 매번 켜기 (터미널 4개, 저장소 맨 위에서)

```bash
# 시장 MCP (8002) — 인증키 없이 기록 데이터로
MARKET_DATA_MODE=replay uv run --no-sync --package market-mcp market-mcp

# Gmail MCP (8001) — 가상 메일함 (실제 메일이 나가지 않음)
GMAIL_ADAPTER=fake uv run --no-sync --package gmail-mcp gmail-mcp

# API (8000)
uv run --no-sync python -m uvicorn main:app --app-dir apps/api/src --port 8000 --env-file .env

# 화면 (5173)
npm --prefix apps/web run dev
```

http://localhost:5173 에서 로그인합니다. `/api` 요청은 Vite 프록시가 8000 으로 넘깁니다.
실제 ECOS·실제 Gmail 로 띄우는 법은 [mcp-servers/market/README.md](mcp-servers/market/README.md), [mcp-servers/gmail/README.md](mcp-servers/gmail/README.md).

### 시연 질문 예시

| 에이전트(템플릿) | 질문 |
|---|---|
| 규정 안내 | 당좌예금 개설할 때 필요한 서류는? · 고객확인의무는 언제 해야 해? · 우주정거장 건설자금 대출 규정 (근거 없음이 정상) |
| 금리·환율 리포트 | 2026-11-18 금리·환율 리포트 만들어 줘 · 2026-11-18 원달러 환율 얼마야? |
| 만기 대출 정리표 | 30일 이내 만기 대출 정리표 만들어 줘 |
| 첨부 (parse_doc 있는 에이전트) | 파일을 올리고 "요약해 줘" · "이 신청서가 우리 서류 보완 규정에 맞는지 봐 줘" |
| 메일 (send_mail 있는 에이전트) | 김도영에게 내일 10시 회의 일정 메일 보내 줘 (승인 카드 후 발송) · 휴가 신청 메일 초안 써 줘 |

규정 안내는 **DB 에 넣은 문서(`DOC_STORE=db`)** 로만 답합니다. 넣지 않은 내용을 물으면 "찾지 못했습니다"가 정상입니다.

## 설정 (.env 주요 항목)

전체 목록과 설명은 [.env.example](.env.example).

| 항목 | 기본 | 설명 |
|---|---|---|
| `LLM_MODEL` | `qwen3:8b` | 채팅 모델. 시연은 `qwen3:8b-24k` |
| `DRAFT_LLM_MODEL` | (비우면 LLM_MODEL) | 에이전트 초안 만들기 모델. 예시 값은 `qwen3:14b` |
| `DATABASE_URL` | | PostgreSQL (`postgresql+asyncpg://…`) |
| `EMBEDDING_MODEL_PATH` | | KURE-v3 폴더 |
| `AUTH_MODE` | `dev` | `session` = 로그인 화면(DB 사용자), `dev` = 로그인 없이 개발용 사용자 선택 |
| `DOC_STORE` | `file` | 문서 검색 위치. `db` = 팀 DB(pgvector), `file` = `data/fixtures/doc_chunks.json` 예시 |
| `DOC_EVIDENCE_MIN_SCORE` | `0` | 규정 안내가 근거로 받는 최소 검색 점수. 0 = 거르지 않음 (11/24 측정: 0.30 은 정답까지 버려서 뺌) |
| `HARNESS_PLANNER` | `1` | 플래너 하네스 켜기 (`0` 이면 예전 루프) |
| `HARNESS_PLANNER_TIMEOUT_SECONDS` | `60` | 플래너 대기 시간. 넘으면 규칙만으로 계획 |
| `HARNESS_MAX_TURNS` | `10` | 한 요청의 LLM 호출 상한 (계획·단계·답 합계) |
| `HARNESS_TOOL_TIMEOUT_SECONDS` | `30` | 도구 1회 제한 시간 |
| `PERMISSION_POLICY_PATH` | `config/permissions.yaml` | 도구 권한 정책 (allow·ask·deny) |
| `GMAIL_MCP_URL` · `MARKET_MCP_URL` | | MCP 서버 주소 (비우면 그 도구를 등록하지 않음) |
| `SCHEDULER_ENABLED` | | `true` 면 `config/schedules.yaml` 시각에 예약 작업 실행 |

## 테스트

```bash
uv run pytest                      # 백엔드 전체 (DB 테스트는 .env 의 DATABASE_URL 이 있을 때만, 트랜잭션 안에서 되돌림)
uv run pytest tests/test_planner.py
(cd apps/web && node --test tests/)
```

`RUN_OLLAMA_TESTS=1` 이면 실제 Ollama 를 부르는 테스트도 돈다.

## 알아 둘 점

- **메모리 (16GB 맥 기준)**: `qwen3:14b`(초안) + `qwen3:8b-24k`(채팅) + KURE-v3 가 한꺼번에 올라가면 스왑이 커져
  문서 검색이 도구 제한 시간(30초)에 걸릴 수 있다. 시연 때는 초안 만들기 뒤 몇 분 기다리거나 초안 모델을 8b 로 둔다.
- **응답 시간**: 플래너 계획에 요청마다 20~45초가 더 든다 (qwen3:8b, M4 16GB 실측).
- **Gmail** 은 외부 서비스다 (기획서 NFR-01 기준 예외, 팀에서 임시 사용으로 수용). 가상 메일함 모드로 시연할 수 있다.
- 시장 데이터 기록 재생(replay) 값은 전달받은 기록 파일의 값이며 현재 수치가 아니다. 화면에 기록 재생이라고 표시된다.

## 문서

- 기획서·요구사항: `docs/3팀_프로젝트_기획서.docx`, `docs/요구사항_정의서_업무자동화에이전트.xlsx`
- ERD: `docs/architecture/erd.sql` (ERDCloud 용 MySQL 문법), 실제 스키마 `docs/architecture/schema.postgres.sql`
- 결정 사항: `docs/adr/`
- 하네스 계약: `contracts/harness.py`, 권한 계약: `contracts/permission.py`
