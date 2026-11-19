# gmail — 팀 Gmail 계정 커넥터

팀 Gmail 계정 하나를 **공용 메일함**으로 연결합니다. 두 가지 일을 합니다.

| 구분 | 무엇 | 실행 |
|---|---|---|
| **MCP 툴** (에이전트가 사용) | `search_mail` · `read_mail` · `send_mail` | `uv run gmail-mcp` |
| **동기화** (스케줄러가 실행, LLM 툴 아님) | 증분 동기화 → 문서 처리 파트에 넘길 메일 문서, 권한 동기화, 삭제 감지 | `uv run gmail-sync` |

> ⚠ 기획서 NFR-01("문서·대화를 외부 API로 보내지 않음") 기준으로 Gmail 은 외부 서비스입니다.
> 이 커넥터를 시연용으로 쓸지, 운영 시 사내 메일 서버 어댑터로 바꿀지는 팀 합의가 필요합니다.
> `client.py` 의 `GmailClient` 인터페이스만 맞추면 다른 메일 시스템 어댑터로 교체할 수 있습니다.

## 실행 순서 (저장소 루트에서)

| 순서 | 어디서 | 명령 | 확인 |
|---|---|---|---|
| 1 | 터미널 1 (처음 한 번) | `uv sync --all-packages --system-certs` | 의존성 설치 |
| 2 | 터미널 1 (처음 한 번 · 7일마다) | `uv run gmail-mcp-auth` (재인증은 `--force`) | 출력된 이메일이 팀 계정인지 |
| 3 | 터미널 1 | `uv run gmail-mcp` | `[gmail-mcp] 모드: 팀 Gmail 계정` |
| 4 | 브라우저 | `http://127.0.0.1:8000/` | 상태 안내 페이지 · "정상 실행 중" |
| 5 | 터미널 2 | `npx @modelcontextprotocol/inspector` | 브라우저에 Inspector 열림 (`localhost:6274`) |
| 6 | Inspector | gmail 카드 토글 켜기 (처음이면 Add Servers → Add manually → streamable-http, `http://127.0.0.1:8000/mcp`) | `● Connected` |
| 7 | Inspector | Tools 탭 → 툴 선택 → 값 입력 → Execute Tool | 결과 표시 |
| 8 | 각 터미널 | `Ctrl+C` | 종료 |

- 가상 메일함으로 띄우려면 PowerShell 에서 `$env:GMAIL_ADAPTER="fake"; uv run gmail-mcp`.
  이 값은 터미널을 닫을 때까지 남으므로, 다시 실제 모드로 띄울 때는 `Remove-Item Env:GMAIL_ADAPTER` 후 실행합니다.
- 사내망 인증서 오류로 Inspector 가 안 뜨면 `$env:NODE_OPTIONS="--use-system-ca"; npx @modelcontextprotocol/inspector`.

## 권한 원칙
- 팀 메일함 하나를 쓰지만, 직원은 **자신이 참여한(From·To·Cc·Bcc) 메일만** 검색·열람합니다.
- 요청자 이메일은 LLM이 아니라 플랫폼이 로그인 사용자로 채웁니다 (`manifest.yaml` 의 `inject`).
- Gmail 검색어에 참여자 조건을 붙이고, 결과를 받은 뒤 참여자 여부를 **한 번 더** 확인합니다.
- 권한이 없거나 없는 메일은 같은 문구("메일을 찾을 수 없거나 열람 권한이 없습니다")로 응답해 존재 여부를 숨깁니다.
- `send_mail` 은 risk high (ASK 권장). 답장 주소(Reply-To)는 요청한 직원, 본문 끝에 요청자 안내, `X-Agent-Requester` 헤더에 사번을 남깁니다.

## 팀 계정 설정 (최초 1회, 팀 계정 담당자가 직접)
1. **팀 Google 계정**으로 https://console.cloud.google.com 에 로그인해 프로젝트 생성
2. API 라이브러리에서 **Gmail API** 사용 설정
3. OAuth 동의 화면: 사용자 유형 *외부*, **테스트 사용자에 팀 계정** 추가
4. 사용자 인증 정보 → OAuth 클라이언트 ID → 애플리케이션 유형 **데스크톱 앱** → JSON 다운로드
5. 다운로드한 파일을 `mcp-servers/gmail/.secrets/credentials.json` 으로 저장 (`.secrets/` 는 커밋되지 않음)
6. 인증 — 브라우저가 열리면 **팀 계정**으로 로그인·승인합니다
   ```bash
   uv run gmail-mcp-auth
   ```
   마지막에 출력되는 이메일이 팀 계정인지 확인하세요. 다른 계정이면 `uv run gmail-mcp-auth --force` 로 다시 인증합니다.

> 테스트(MCP_test_project)에서 쓰던 개인 계정의 `credentials.json` · `token.json` 은 가져오지 않습니다.
> 테스트 모드 OAuth 앱의 리프레시 토큰은 7일 뒤 만료되므로, 만료되면 `--force` 로 다시 인증합니다.

환경변수 (`.env`)
| 변수 | 기본값 | 설명 |
|---|---|---|
| `GMAIL_ADAPTER` | `google` | `fake` 면 가상 메일함 (팀 계정 없이 시연·테스트) |
| `GMAIL_CREDENTIALS_FILE` | `mcp-servers/gmail/.secrets/credentials.json` | OAuth 클라이언트 파일 |
| `GMAIL_TOKEN_FILE` | `mcp-servers/gmail/.secrets/token.json` | 인증 토큰 |
| `GMAIL_ALLOWED_RECIPIENT_DOMAINS` | (제한 없음) | 쉼표로 구분한 허용 수신 도메인 |
| `GMAIL_MAX_RECIPIENTS` | `10` | 받는 사람 + 참조 최대 수 |
| `GMAIL_SYNC_DIR` | `data/sync/gmail` | 동기화 결과·상태 저장 위치 |
| `GMAIL_MCP_TOKEN` | — | 플랫폼 → MCP 서버 접속 토큰 (manifest 의 `auth_token_env`) |

## 동기화 설계 (Onyx 참고)

| 기능 | 방식 | 참고한 Onyx 코드 |
|---|---|---|
| 증분 동기화 | 마지막 성공 시각 ~ 지금 창을 `after:<epoch> before:<epoch>` 로 검색. 시작 시각을 10분 앞당겨 경계 누락 방지 (중복은 doc_id 덮어쓰기로 무해) | Gmail 커넥터의 시간 범위 검색, Confluence 커넥터의 시간 창 · 여유 구간 |
| 중단 후 재개 | 목록 1페이지 = 체크포인트 1회. 페이지마다 `state.json` 에 다음 페이지 토큰 저장 | Gmail 커넥터 `PAGES_PER_CHECKPOINT`, Confluence `next_page_url` 체크포인트 |
| 문서 단위 | 스레드 1개 = 문서 1개 (`gmail:<thread_id>`). 새 답장이 오면 스레드 전체를 다시 가져옴 | Gmail 커넥터 `thread_to_document` |
| 실패 격리 | 스레드 하나가 실패해도 `SyncFailure` 로 기록하고 계속 | `ConnectorFailure` |
| 권한 | 참여자 전원 + 팀 계정 (`MailAccess`) | `ExternalAccess` (Onyx 는 메일함 소유자 기준 — 공용 메일함이라 참여자 기준으로 바꿈) |
| 권한 동기화 | 본문 없이 헤더만 읽는 슬림 목록으로 문서별 권한을 다시 계산 | 슬림 문서 기반 권한 동기화 흐름 |
| 삭제 감지 | 전체 슬림 목록과 색인을 비교. 페이지 토큰 반복·최대 페이지 초과 시 부분 결과로 끝내지 않고 중단 (멀쩡한 문서 삭제 방지) | Gmail 커넥터 슬림 목록 가드 |

```bash
uv run gmail-sync               # 증분 동기화
uv run gmail-sync --full        # 전체 다시 동기화
uv run gmail-sync --perm-sync   # 권한 동기화
```
결과: `documents/<job_id>.jsonl`(MailDocument), `failures/…`, `permissions/…`, `state.json`.
문서 처리 파트(파싱·청킹)로 넘기는 형식은 아직 합의 전입니다 — `MailDocument.to_text()` 와 `access` 를 넘기는 안을 제안합니다.

## 출처 · 라이선스
- 설계 참고: [Onyx](https://github.com/onyx-dot-app/onyx) `backend/onyx/connectors/gmail/connector.py`,
  `backend/onyx/connectors/confluence/connector.py` (MIT Expat). 구조만 참고했고 코드는 새로 작성했습니다.
- Onyx 권한 동기화(`backend/ee/onyx/external_permissions/gmail/doc_sync.py`)는 **Onyx Enterprise License** 이므로
  코드를 사용하지 않았고, 흐름만 참고해 `perm_sync.py` 를 직접 작성했습니다.

## 구조
```
gmail/
├── manifest.yaml        search_mail · read_mail (read/medium), send_mail (write/high)
├── src/gmail_mcp/
│   ├── server.py        MCP 툴 정의
│   ├── service.py       권한 확인 · 입력 검증 · 발송 메시지 구성
│   ├── client.py        Gmail API 계층 (GoogleGmailClient / 인터페이스)
│   ├── fake.py          가상 메일함 (테스트 · 시연)
│   ├── google_auth.py   팀 계정 OAuth (gmail-mcp-auth)
│   ├── convert.py       Gmail 원본 → MailDocument
│   ├── sync.py          증분 동기화 · 슬림 목록
│   ├── perm_sync.py     권한 동기화 · 삭제 감지
│   ├── runner.py        동기화 실행기 (gmail-sync)
│   └── models.py, config.py
└── tests/
```
