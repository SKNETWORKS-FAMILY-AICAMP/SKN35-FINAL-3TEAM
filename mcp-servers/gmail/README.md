# gmail — 직원 개인 Gmail 커넥터

직원마다 **자기 Google 계정**을 연결하고, 메일을 **관계형 DB에만** 저장합니다 (벡터DB · 청킹 대상 아님).

| 구분 | 무엇 | 실행 |
|---|---|---|
| **MCP 툴** (에이전트가 사용) | `search_mail` · `read_mail` · `send_mail` | `uv run --package gmail-mcp gmail-mcp` |
| **동기화** (스케줄러가 실행, LLM 툴 아님) | 직원별 메일함 → `mail_message` 테이블, 삭제 감지 | `uv run --package gmail-mcp gmail-sync --user <사번>` / `--all` |
| **계정 연결** (직원별 1회) | 직원이 자기 Google 계정으로 로그인 | `uv run --package gmail-mcp gmail-mcp-auth --user <사번>` |

> ⚠ 기획서 NFR-01("문서·대화를 외부 API로 보내지 않음") 기준으로 Gmail 은 외부 서비스입니다 (팀에서 임시 사용으로 수용).
> `client.py` 의 `GmailClient` 인터페이스만 맞추면 사내 메일 시스템 어댑터로 교체할 수 있습니다.

## 실행 순서 (저장소 루트에서)

| 순서 | 어디서 | 명령 | 확인 |
|---|---|---|---|
| 1 | 터미널 1 (처음 한 번) | `uv sync --all-packages --system-certs` | 의존성 설치 |
| 2 | 터미널 1 (직원별 처음 한 번 · 7일마다) | `uv run --package gmail-mcp gmail-mcp-auth --user E1001` (재인증은 `--force`) | `연결 완료: E1001 ↔ 본인 메일` |
| 3 | 터미널 1 | `uv run --package gmail-mcp gmail-mcp` | `[gmail-mcp] 모드: 직원 개인 Gmail 계정` |
| 4 | 브라우저 | `http://127.0.0.1:8001/` | 상태 안내 페이지 · "정상 실행 중" |
| 5 | 터미널 2 | `npx @modelcontextprotocol/inspector` | 브라우저에 Inspector 열림 (`localhost:6274`) |
| 6 | Inspector | gmail 카드 토글 켜기 (처음이면 Add Servers → Add manually → streamable-http, `http://127.0.0.1:8001/mcp`) | `● Connected` |
| 7 | Inspector | Tools 탭 → 툴 선택 → `requester_id` 에 2단계 사번 입력 → Execute Tool | 결과 표시 |
| 8 | 각 터미널 | `Ctrl+C` | 종료 |

- 가상 메일함으로 띄우려면 PowerShell 에서 `$env:GMAIL_ADAPTER="fake"; uv run --package gmail-mcp gmail-mcp` (가상 사번: `E1001`, `E2002`).
  이 값은 터미널을 닫을 때까지 남으므로, 실제 모드로 띄울 때는 `Remove-Item Env:GMAIL_ADAPTER` 후 실행합니다.
- 사내망 인증서 오류로 Inspector 가 안 뜨면 `$env:NODE_OPTIONS="--use-system-ca"; npx @modelcontextprotocol/inspector`.

## 권한 원칙
- **메일함 주인 = 그 메일을 볼 수 있는 유일한 사람.** 검색 · 읽기는 관계형 DB 에서 `owner_user_id = 요청자` 인 메일만 봅니다.
  다른 직원의 `thread_id` 를 알아도 열 수 없습니다.
- 저장소(`store.py`)의 모든 함수는 `owner_user_id` 를 **필수**로 받습니다. 주인 조건 없이 메일을 읽는 함수가 없습니다.
- 요청자 사번은 LLM 이 아니라 플랫폼이 로그인 사용자로 채웁니다 (`manifest.yaml` 의 `inject`).
- 발송은 **요청자 본인 계정**으로 합니다. 본문 끝에 "사내 AI 에이전트를 통해 발송된 메일입니다", `X-Agent-Requester` 헤더에 사번.
- 메일은 벡터DB · `chunk` 에 넣지 않으므로 `chunk.owner_user_id` 같은 ERD 변경은 메일 때문에는 필요 없습니다.
- 권한이 없거나 없는 메일은 같은 문구("메일을 찾을 수 없거나 열람 권한이 없습니다")로 응답해 존재 여부를 숨깁니다.

## FastAPI · React에서 사용

저장소 루트에서 `uv sync --all-packages --extra hwp`로 설치합니다. `hwp`는 바이너리 HWP 파싱용 선택 의존성입니다. MCP 서버는 8001, FastAPI는 8000을 사용합니다.

가상 메일함을 사용하는 Windows CMD 예시입니다. 첫 번째 창에서 MCP 서버를 실행합니다.

```cmd
set "GMAIL_ADAPTER=fake"
uv run --package gmail-mcp gmail-mcp
```

두 번째 CMD에서 FastAPI를 실행합니다.

```cmd
set "GMAIL_MCP_URL=http://127.0.0.1:8001/mcp"
set GMAIL_MCP_USER_MAP={"kim":"E1001","park":"E2002"}
set "LLM_PROVIDER=openai"
set "LLM_MODEL=qwen3:8b"
uv run python -m uvicorn main:app --app-dir apps/api/src
```

React의 도구 목록에 `search_mail`, `read_mail`, `send_mail`이 기본으로 표시됩니다. 기본 MCP 주소는 `http://127.0.0.1:8001/mcp`이며 에이전트에 필요한 도구를 선택하고 새 대화에서 사용합니다. 도구를 실행하려면 MCP 서버도 실행되어 있어야 합니다. `GMAIL_MCP_URL`을 명시적으로 빈 값으로 설정하면 메일 도구 등록을 끕니다. `GMAIL_MCP_USER_MAP`은 서버 설정이며, LLM 입력에는 `requester_id`가 없습니다. 실제 계정 연결을 `kim`으로 했다면 매핑 없이 `kim`이 사용됩니다.

`send_mail`은 관리자도 기존 승인 화면에서 확인한 뒤 실행됩니다. fake 모드의 결과는 `status=simulated`이며 실제 메일은 발송되지 않습니다. 연결 실패 시 발송을 자동 재시도하지 않습니다. 응답 유실 뒤 중복 발송을 피하려면 보낸편지함을 먼저 확인합니다.

실제 Gmail은 첫 번째 CMD에서 `GMAIL_ADAPTER=google`로 설정하고 해당 사용자 ID로 계정을 연결해야 합니다. MCP 프로세스와 FastAPI 프로세스 양쪽에 같은 `GMAIL_MCP_TOKEN`을 설정하면 `/mcp` 요청에 Bearer 토큰 검사가 적용됩니다. Inspector에도 같은 Authorization 헤더가 필요합니다. 토큰은 비밀 값으로 관리합니다.

### 로그인 담당자와 연결할 부분

플랫폼 로그인과 Gmail OAuth 연결은 별개입니다. Gmail 주소를 사용자 정보에 저장하는 것만으로는 발송할 수 없습니다.
Gmail MCP가 그 사용자의 `gmail.readonly`·`gmail.send` 권한을 가진 OAuth 자격증명을 읽을 수 있어야 합니다.
현재 MCP는 `GMAIL_TOKENS_DIR/<mailbox_user_id>.json`을 읽습니다. 토큰을 DB에 저장하는 구현으로 바꾸면
`google_auth.load_credentials()`도 그 저장소를 읽도록 연결해야 합니다. 토큰은 LLM·프런트·일반 로그에 넘기지 않습니다.

플랫폼 측 연결 지점은 `build_from_env(gmail_account_resolver=...)`입니다.
다음은 로그인 담당자의 계정 저장소에 맞춰 채울 연결 예시입니다(아직 존재하지 않는 `account_repository`는 팀원 구현으로 대체).

```python
from main import create_app
from services.container import build_from_env
from services.mail_accounts import GmailAccountLink

async def resolve_gmail_account(user_id: str) -> GmailAccountLink | None:
    row = await account_repository.find_active_gmail_link(user_id)
    if row is None:
        return None
    return GmailAccountLink(
        platform_user_id=str(row.user_id),
        mailbox_user_id=str(row.mailbox_user_id),
    )

app = create_app(build=lambda: build_from_env(gmail_account_resolver=resolve_gmail_account))
```

- `routers/deps.py:current_user()`는 로그인 담당자가 인증 세션/JWT를 검증해 얻은 `Principal`을 반환하도록 교체합니다.
  현재 `X-User-Id` 선택 방식은 개발용입니다.
- `platform_user_id`는 그 `Principal.user_id`와 같아야 합니다. DB의 정수 사용자 ID도 `Principal`에는 문자열로 통일합니다.
- `mailbox_user_id`는 MCP 계정·토큰의 소유자 키입니다. 같은 사용자 ID를 쓰면 관리가 단순합니다.
  현재 허용 형태는 영문·숫자·밑줄·하이픈 1~64자이며 **Gmail 주소를 이 칸에 넣지 않습니다**.
- 연결 테이블에는 사용자 ID, Gmail 주소(표시용), mailbox 소유자 키, 연결 상태를 묶어 저장합니다.
  `None`(미연결/연결 해제) 또는 조회 실패면 MCP를 호출하지 않습니다. 매 호출마다 다시 조회합니다.
- resolver를 주면 `GMAIL_MCP_USER_MAP`이나 기본 계정으로 대체하지 않습니다. resolver를 주지 않는 기존 개발 실행은 그대로 동작합니다.
- 에이전트 입력은 수신자·참조·제목·본문뿐입니다. 발신자·`requester_id`는 입력할 수 없으며
  실제 발신 계정은 인증된 사용자의 OAuth 자격증명으로 정해집니다.
- 기존 채팅 내 발송 확인은 유지합니다. 로그인 구현이 붙어도 승인 전 발송하지 않습니다.

통합 확인: 서로 다른 두 사용자로 발송 계정이 나뉘는지, 미연결/연결 해제 계정이 차단되는지,
다른 사용자 연결 정보를 반환하면 거절되는지, 실제 모드에서 발송 결과가 `sent`로 오는지를 확인합니다.
가상 테스트의 `simulated`는 실제 Gmail 전송 성공을 뜻하지 않습니다.

파서 도구 `parse_doc`도 같은 플랫폼 목록에 등록됩니다. 해당 도구를 선택한 에이전트의 작업 채팅에서 클립 버튼으로 문서를 첨부하고 요청을 전송합니다. 파일은 `UPLOAD_DIR`(기본 `data/uploads`)에 임의 ID로 저장하고 SQLite 메타데이터로 소유자를 검사합니다. 폴더에 수동 복사한 파일은 등록된 첨부가 아니므로 채팅에서 다시 업로드해야 합니다. PDF·스캔은 사전에 준비한 모델 경로를 `PARSER_MODELS_DIR`로 지정해야 하며 실행 중 모델을 다운로드하지 않습니다.

검증 명령입니다. 실제 Gmail 계정 대신 가상 메일함을 사용합니다.

```cmd
uv run --all-packages python -X utf8 -m pytest tests mcp-servers/gmail/tests -q
```

## Google Cloud 설정 (플랫폼 담당자 1회)
1. https://console.cloud.google.com 에서 플랫폼용 프로젝트 생성, **Gmail API** 사용 설정
2. OAuth 동의 화면: 사용자 유형 *외부*, **테스트 사용자에 메일을 연결할 직원 계정을 모두 추가** (테스트 상태는 최대 100명)
3. OAuth 클라이언트 ID → **데스크톱 앱** → JSON 을 `mcp-servers/gmail/.secrets/credentials.json` 으로 저장
4. 직원마다 `uv run --package gmail-mcp gmail-mcp-auth --user <사번>` → 브라우저에서 **본인 계정**으로 로그인·승인
   → 토큰은 `.secrets/tokens/<사번>.json`, 사번 ↔ 메일 주소는 DB `mail_account` 에 기록

> 테스트 상태 OAuth 앱의 리프레시 토큰은 7일 뒤 만료됩니다. 만료되면 `--force` 로 다시 연결합니다.
> 웹 화면이 생기면 같은 흐름을 웹 OAuth(로그인 후 리디렉트)로 바꾸고, 토큰은 암호화해 DB 에 보관합니다.

환경변수 (`.env`)
| 변수 | 기본값 | 설명 |
|---|---|---|
| `GMAIL_ADAPTER` | `google` | `fake` 면 가상 메일함 (실제 계정 없이 시연·테스트) |
| `GMAIL_CREDENTIALS_FILE` | `mcp-servers/gmail/.secrets/credentials.json` | 플랫폼 OAuth 클라이언트 |
| `GMAIL_TOKENS_DIR` | `mcp-servers/gmail/.secrets/tokens` | 직원별 토큰 `<사번>.json` |
| `GMAIL_DB_PATH` | `data/db/mail.sqlite3` | 메일 DB (개발용 SQLite) |
| `GMAIL_DATABASE_URL` | (없음) | 플랫폼 PostgreSQL 주소 (`postgresql://사용자:비밀번호@호스트:5432/DB`). 있으면 SQLite 대신 이 DB 의 `mail_*` 표에 저장. 실제 값은 커밋하지 않음 |
| `GMAIL_SYNC_ON_SEARCH` | `true` | `search_mail` 전에 그 직원의 새 메일을 먼저 가져옴 |
| `GMAIL_SEARCH_SYNC_MAX_PAGES` | `2` | 검색 전 동기화 최대 페이지 (툴 30초 제한 안에서) |
| `GMAIL_ALLOWED_RECIPIENT_DOMAINS` | (제한 없음) | 쉼표로 구분한 허용 수신 도메인 |
| `GMAIL_MAX_RECIPIENTS` | `10` | 받는 사람 + 참조 최대 수 |
| `GMAIL_MAX_MESSAGE_BYTES` | `26214400` (25MB) | 이보다 큰 메일은 `too_large` 로 기록 |
| `GMAIL_PRUNE_MAX_RATIO` · `GMAIL_PRUNE_MIN_DOCS` | `0.3` · `20` | 삭제 비율 가드 |
| `GMAIL_MCP_TOKEN` | — | 플랫폼 → MCP 서버 접속 토큰 (manifest 의 `auth_token_env`) |

## 저장 구조 (관계형 DB)
표 5개는 플랫폼 ERD 에 추가돼 있습니다 (`alembic/versions/0011_mail_tables.py` **[초안, DB 담당 확인 전]**, `apps/api/src/db/models.py`, `docs/architecture/erd.sql`). 읽기용 사본: [`sql/mail_schema.sql`](sql/mail_schema.sql) (PostgreSQL 문법).

| 쓰는 곳 | 설정 | 저장소 |
|---|---|---|
| 플랫폼 (운영·통합) | `GMAIL_DATABASE_URL` 설정 | PostgreSQL 16 `mail_*` 표 (`store_pg.py`) |
| 개발·테스트 | 설정 없음 | SQLite 파일 `GMAIL_DB_PATH` (`store.py`) |

- PostgreSQL 사용 전에 `uv run alembic upgrade head` 로 표를 만들고, 커넥터 의존성에 `asyncpg` 가 필요합니다 (`uv sync --all-packages` 에 포함, 단독 설치는 `gmail-mcp[postgres]`).
- **사용자 연결**: 툴이 받는 `requester_id` 는 `users.login_id` 입니다. 저장소가 SQL 안에서 `users.id` 로 바꿔 `owner_user_id` 조건으로 씁니다.
  `users` 에 없는 로그인 ID 는 아무것도 읽지 못하고 계정 연결도 거부됩니다.
  → `gmail-mcp-auth --user <login_id>` 의 값도 같은 로그인 ID 로 맞춥니다. 로그인이 `users` 표로 바뀌면 하네스의 `GMAIL_MCP_USER_MAP` 은 필요 없습니다.

### 퇴사(삭제)한 직원의 메일 — 관리자 보관
직원이 `users` 에서 삭제되면 DB 트리거(`alembic 0012`)가 **지우기 직전에** 그 직원의 메일을 `mail_archive` 로 복사하고, 원래 표(`mail_*`)에서는 지웁니다.
앱이 어떤 경로로 직원을 지워도 DB 가 보관하므로 코드에서 빼먹을 수 없습니다.

- 보관 행에는 삭제 당시 `former_user_id` · `former_login_id` · `former_name` · `former_dept_id` · 연결했던 메일 주소(`account_email`)가 같이 적힙니다.
  직원이 이미 없으므로 `users` 외래키는 없습니다. `users.id` 는 다시 쓰이지 않아 `former_user_id` 로 구분됩니다.
- **에이전트(MCP 툴)는 보관 메일을 읽을 수 없습니다.** 같은 로그인 ID 를 새 직원이 받아도 퇴사자의 메일은 보이지 않습니다.
- 관리자는 CLI 로만 봅니다 (`GMAIL_DATABASE_URL` 필요):
  ```powershell
  uv run gmail-archive list                       # 보관된 직원과 메일 수
  uv run gmail-archive search <직원> [검색어]     # <직원> = former_user_id 또는 삭제 당시 로그인 ID
  uv run gmail-archive read <직원> <thread_id>    # 스레드 읽기
  uv run gmail-archive purge <직원> --yes         # 영구 삭제 (보존 기간 후, 되돌릴 수 없음)
  ```
- 보관하지 않는 것: `mail_account` · `mail_sync_*` (그대로 삭제). **Gmail 연결 토큰 파일(`.secrets/tokens/<로그인ID>.json`)은 따로 지워야 합니다.**
- 보존 기간 · 열람 기록(누가 퇴사자 메일을 봤는지)은 아직 정해지지 않았습니다 (정책 합의 필요).

- 저장소 두 개를 같은 테스트로 검증합니다. PostgreSQL 쪽은 쓰고 지워도 되는 DB 를 지정할 때만 돕니다:
  `$env:GMAIL_TEST_DATABASE_URL="postgresql://사용자@127.0.0.1:5432/테스트DB"; uv run pytest mcp-servers/gmail`

| 테이블 | 내용 |
|---|---|
| `mail_account` | 사번 ↔ 연결한 메일 주소 |
| `mail_message` | 메일 한 통 = 한 행. 기본키 `(owner_user_id, message_id)`. 본문 텍스트 포함 |
| `mail_sync_state` | 직원별 마지막 성공 시각 · 진행 중 체크포인트 |
| `mail_sync_failure` | 실패 사유 코드 (`fetch_error` · `too_large` · `invalid_message`) |
| `mail_sync_run` | 실행마다 건수 기록 (감사용) |

## 동기화 설계 (Onyx 참고)

| 기능 | 방식 | 참고한 Onyx 코드 |
|---|---|---|
| 증분 동기화 | 직원별 마지막 성공 시각 ~ 지금 창을 `after:<epoch> before:<epoch>` 로 검색. 시작 시각을 10분 앞당겨 경계 누락 방지 | Gmail 커넥터의 시간 범위 검색, Confluence 커넥터의 시간 창 · 여유 구간 |
| 중단 후 재개 | 목록 1페이지 = 체크포인트 1회. 페이지마다 `mail_sync_state` 에 다음 페이지 토큰 저장 | Gmail 커넥터 `PAGES_PER_CHECKPOINT`, Confluence `next_page_url` 체크포인트 |
| 변경 판정 | 저장 내용 해시가 같으면 DB 를 쓰지 않음 (new / updated / 변경 없음) | — |
| 실패 격리 | 한 통이 실패해도 사유 코드와 함께 기록하고 계속. 큰 메일은 건너뛰지 않고 `too_large` | `ConnectorFailure` |
| 권한 | 메일함 주인 한 명 (Onyx 와 같은 전제) — 권한이 바뀌지 않으므로 권한 동기화는 두지 않음 | `ExternalAccess` (메일함 소유자 기준) |
| 삭제 감지 | `--prune`: 메일 ID 전체 목록과 DB 를 비교해 삭제. 목록이 불완전하면 중단, 삭제 비율 30% 초과 시 반영하지 않고 멈춤. 가져올 때 사라진 메일도 삭제로 처리 | Gmail 커넥터 슬림 목록 가드 |

```bash
uv run --package gmail-mcp gmail-sync --user E1001                  # 증분 동기화
uv run --package gmail-mcp gmail-sync --all                         # 연결한 모든 직원
uv run --package gmail-mcp gmail-sync --user E1001 --full           # 전체 다시 확인 (바뀐 것만 씀)
uv run --package gmail-mcp gmail-sync --user E1001 --prune          # 삭제 감지
uv run --package gmail-mcp gmail-sync --user E1001 --prune --force  # 비율 가드를 넘어도 반영 (사람이 확인한 뒤에만)
```

## 출처 · 라이선스
- 설계 참고: [Onyx](https://github.com/onyx-dot-app/onyx) `backend/onyx/connectors/gmail/connector.py`,
  `backend/onyx/connectors/confluence/connector.py` (MIT Expat). 구조만 참고했고 코드는 새로 작성했습니다.
- Onyx 권한 동기화(`backend/ee/...`)는 Onyx Enterprise License 라 코드를 쓰지 않았고,
  개인 메일함 방식에서는 권한이 바뀌지 않아 권한 동기화 자체를 두지 않습니다.

## 구조
```
gmail/
├── manifest.yaml        search_mail · read_mail (read/medium), send_mail (write/high), inject requester_id
├── sql/mail_schema.sql  PostgreSQL 테이블 정의 (읽기용 사본 — 실제 생성은 alembic 0011)
├── src/gmail_mcp/
│   ├── server.py        MCP 툴 정의 · 상태 페이지
│   ├── service.py       본인 메일만 조회 · 입력 검증 · 본인 계정 발송
│   ├── store.py         관계형 DB 저장소 SQLite (모든 함수가 owner_user_id 필수) · open_store
│   ├── store_pg.py      관계형 DB 저장소 PostgreSQL (같은 메서드, 사용자는 users.login_id 로 연결) + 퇴사자 보관 조회
│   ├── archive.py       관리자용 퇴사자 메일 보관 CLI (gmail-archive)
│   ├── accounts.py      사번 → 그 직원의 메일함 클라이언트
│   ├── google_auth.py   직원별 OAuth (gmail-mcp-auth --user)
│   ├── client.py        Gmail API 계층 (GoogleGmailClient / 인터페이스)
│   ├── fake.py          가상 메일함 (테스트 · 시연)
│   ├── convert.py       Gmail 원본 → MailMessage · 본문 추출 · 해시
│   ├── sync.py          증분 동기화 · 메일 ID 목록
│   ├── runner.py        동기화 실행기 (gmail-sync)
│   └── models.py, config.py, status_page.py
└── tests/
```
