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
| `GMAIL_SYNC_ON_SEARCH` | `true` | `search_mail` 전에 그 직원의 새 메일을 먼저 가져옴 |
| `GMAIL_SEARCH_SYNC_MAX_PAGES` | `2` | 검색 전 동기화 최대 페이지 (툴 30초 제한 안에서) |
| `GMAIL_ALLOWED_RECIPIENT_DOMAINS` | (제한 없음) | 쉼표로 구분한 허용 수신 도메인 |
| `GMAIL_MAX_RECIPIENTS` | `10` | 받는 사람 + 참조 최대 수 |
| `GMAIL_MAX_MESSAGE_BYTES` | `26214400` (25MB) | 이보다 큰 메일은 `too_large` 로 기록 |
| `GMAIL_PRUNE_MAX_RATIO` · `GMAIL_PRUNE_MIN_DOCS` | `0.3` · `20` | 삭제 비율 가드 |
| `GMAIL_MCP_TOKEN` | — | 플랫폼 → MCP 서버 접속 토큰 (manifest 의 `auth_token_env`) |

## 저장 구조 (관계형 DB)
플랫폼 DB 용 테이블 정의: [`sql/mail_schema.sql`](sql/mail_schema.sql) (ERD 추가 제안). 개발 중에는 같은 구조를 SQLite 로 씁니다.

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
├── sql/mail_schema.sql  플랫폼 DB 테이블 정의 (ERD 추가 제안)
├── src/gmail_mcp/
│   ├── server.py        MCP 툴 정의 · 상태 페이지
│   ├── service.py       본인 메일만 조회 · 입력 검증 · 본인 계정 발송
│   ├── store.py         관계형 DB 저장소 (모든 함수가 owner_user_id 필수)
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
