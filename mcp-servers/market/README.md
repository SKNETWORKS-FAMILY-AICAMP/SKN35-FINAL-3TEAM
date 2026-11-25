# 시장 데이터 MCP · UC-02

에이전트 → 플랫폼 `ToolSpec` → **시장 MCP(8002)** → 한국은행 ECOS API.
외부 API 키와 통계 코드 선택은 MCP에 둔다. 에이전트가 URL·인증키·통계 코드를 만들거나 입력하지 않는다.
Gmail MCP(8001)와 별도 프로세스다. **MCP 서버는 조회만 하고 아무것도 저장하지 않는다.**

## 등록 도구

| 도구 | 화면 이름 | 실행 위치 | 역할 |
|---|---|---|---|
| `market_snapshot` | 금리·환율 조회 | **시장 MCP** | USD, JPY(100엔), EUR, CNY, 한국은행 기준금리 조회. 실제 기준일·직전 관측값 대비를 코드로 계산. 받은 결과는 플랫폼이 보관 |
| `search_report_templates` | 보고서 양식 검색 | 플랫폼 | 목적·양식명으로 후보 검색. 현재 DB 없이 로컬 키워드 검색 |
| `get_report_template` | 보고서 양식 전체 읽기 | 플랫폼 | 전체 항목·필수 입력·버전 읽기 및 선택 버전 고정 |
| `save_morning_report` | 아침 리포트 저장 | 플랫폼 | 플랫폼이 보관한 조회 결과·양식 선택·요약으로 작성. 필수 자료 검사 및 요약 검증 |
| `get_morning_report` | 저장한 리포트 읽기 | 플랫폼 | 본인 리포트를 번호로 다시 읽기 |

플랫폼 도구는 이 패키지의 작성 코드(`MarketService`, `report_templates.py`, `report_documents.py`)를 라이브러리로 쓰고,
저장은 플랫폼 문서 저장소(`apps/api/src/services/documents.py`)에 한다 (`apps/api/src/tools/market.py`).
저장한 리포트는 다른 서류와 같이 **내 문서**와 채팅의 PDF·DOCX 버튼으로 받는다 (`GET /api/documents/{id}/download`).

플랫폼에서 로그인 사용자 ID를 `requester_id`에 넣는다. 모델 입력에는 없다.
다른 사용자의 스냅샷으로 저장하거나 다른 사용자의 리포트를 읽을 수 없다.

## 준비 · 저장소 최상위 CMD

```cmd
uv sync --all-packages --extra hwp
```

이 명령은 Gmail·시장 MCP와 HWP 선택 의존성을 함께 준비한다.
이후 아래의 `--no-sync` 실행은 다른 작업 공간 패키지가 제거되지 않도록 이미 준비된 환경을 사용한다.

## 먼저 기록 데이터로 시연하기 — 인증키 불필요

CMD 1, 저장소 최상위에서:

```cmd
set "MARKET_DATA_MODE=replay"
uv run --no-sync --package market-mcp market-mcp
```

`http://127.0.0.1:8002/health`에 `mode: replay`가 표시되면 시작된 것이다.
포함된 자료는 사용자가 전달한 `UC02_전달.zip`의 ECOS 기록 JSON이다.
본문·메타데이터의 SHA-256을 검사해 복사했으며, **내용이 실제 ECOS 응답과 일치하는지 새로 인증한 것은 아니다**.
출력에는 기록 재생이라는 표시가 항상 붙는다. 실시간 조회 실패 시 이 모드로 자동 대체하지 않는다.

CMD 2, FastAPI:

```cmd
uv run --no-sync python -m uvicorn main:app --app-dir apps/api/src --reload
```

프런트는 기존과 같이 `apps/web`에서 `npm run dev`로 실행한다.
‘금리·환율 리포트’ 템플릿으로 새 에이전트를 저장한다. 또는 위 다섯 도구를 직접 선택한다.

시연 요청:

```text
2026-11-18 금리·환율 리포트 만들어 줘.
2026-11-18 간단 환율 브리핑 양식으로 작성하고 저장해 줘.
2026-11-18 금리·환율 점검 보고서로 작성하고 저장해 줘.
```

기록 파일 기준 USD 1,338.80원, JPY 839.45원/100엔, 기준금리 3.00% 등이 표시된다.
기준금리의 실제 관측일은 2026-11-16이며 요청일과 다름을 표시한다.
이 값들은 **전달 파일의 기록값이며 현재 수치 안내가 아니다**.

저장 후에는 다음처럼 조회한다.

```text
리포트 번호 <저장 결과의 32자리 번호>를 다시 열어 줘.
```

조회만 하려면 `2026-11-18 환율 조회해 줘`라고 요청한다. 시장 전용 에이전트에서는 모델 호출 없이 조회 결과를 표시한다.

## 시장 DB 에 쌓고 리포트는 DB 값만 쓰기 (UC-02-1, 추천)

유즈케이스 UC-02-1: 수집기가 ECOS 에서 받은 파일을 **받은 편지함(경계, `MARKET_INBOX_DIR`)** 에 놓고,
적재기가 해시·형식을 검사해 통과한 값만 **시장 DB(마이그레이션 0014: `market_fetch`·`market_value`)** 에 넣는다.

- 해시가 다르면 파일 전체 거부 (3a). 받은 기록은 성공·빈 응답·오류·거부 모두 `market_fetch` 에 남는다.
- 직전 관측값보다 환율이 3% 넘게, 기준금리가 0.5%p 넘게 움직이면 보류(`held`, EXT-1). 리포트에 쓰지 않는다.
- 같은 날짜에 다른 값이 오면 이전 값은 고치지 않고 정정 표시(`superseded`)만 하고 새 값을 쓴다.
- `MARKET_DATA_MODE=db` 면 `market_snapshot` 은 시장 DB 의 통과 값만 읽는다 (리포트에 "사내 시장 DB" 표시).

`mcp-servers/market/.env` 에 `DATABASE_URL`(플랫폼과 같은 DB)과 `ECOS_API_KEY` 를 넣고 MCP 를 띄우면
`collect_market_data` 도구가 열린다. 플랫폼의 예약 실행(평일 09:10, `config/schedules.yaml`)이 이 도구를 부른다.
에이전트 툴 목록에는 들어가지 않는다. 손으로 한 번 돌리려면:

```cmd
uv run --no-sync --env-file mcp-servers/market/.env --package market-mcp market-collect --days 10
```

인증키가 없으면 수집기는 기록 파일(replay)로 받는다 (`MARKET_COLLECT_MODE` 로 고정할 수 있다). 이때 리포트에 "기록 재생" 이 표시된다.

## 실제 ECOS API 사용하기

공식 안내: https://ecos.bok.or.kr/api/

1. ECOS 인증키를 준비한다. 주소 저장이나 MCP 토큰과 별개의 키다.
2. `mcp-servers/market/.env.example`을 **아직 파일이 없을 때만** `.env`로 복사한다.
3. MCP 전용 `.env`에 `ECOS_API_KEY`를 입력한다. 플랫폼 `.env`나 시스템 프롬프트에 넣지 않는다.
4. 기존 MCP를 종료하고 아래 명령으로 시작한다.

```cmd
set "MARKET_DATA_MODE=live"
uv run --no-sync --env-file mcp-servers/market/.env --package market-mcp market-mcp
```

같은 CMD에 replay 환경변수가 남지 않도록 위처럼 모드를 명시한다.
키가 없거나 ECOS가 거부하면 오류로 안내한다. 키가 포함된 요청 URL·예외 원문은 사용자 응답이나 일반 로그에 남기지 않는다.
고정된 `ecos.bok.or.kr` HTTPS 경로만 요청하고 HTTP 리디렉션은 따르지 않는다.

계열 코드는 전달 자료의 정의를 사용한다.

| 계열 | 통계표 | 항목 | 단위 |
|---|---|---|---|
| USD | 731Y001 | 0000001 | 원/1달러 |
| JPY | 731Y001 | 0000002 | 원/100엔 |
| EUR | 731Y001 | 0000003 | 원/1유로 |
| CNY | 731Y001 | 0000053 | 원/1위안 |
| 기준금리 | 722Y001 | 0101000 | 연 % |

인증키를 넣은 전체 5계열 실시간 성공 여부는 해당 환경에서 별도 확인해야 한다.
공식 API의 제공·고시 시각을 보장하지 않는다. 해당 날짜에 값이 없으면 과거 값과 실제 날짜를 표시한다.

## 플랫폼 설정

- `MARKET_MCP_URL`: 기본 `http://127.0.0.1:8002/mcp`. 명시적으로 비우면 도구 등록을 끈다.
- `MARKET_MCP_TOKEN`: 사용하면 플랫폼과 MCP 프로세스 양쪽에 같은 값을 설정한다.
- `ECOS_API_KEY`: **MCP 프로세스에만** 설정한다.
- `MARKET_DATA_MODE`: MCP의 `live` 또는 `replay`.
- 저장 위치: 플랫폼의 `DOCUMENT_DB_PATH`(기본 `data/documents.sqlite3`). 조회 결과·양식 선택·리포트를 소유자와 함께 저장한다.
  (이전의 `MARKET_STORAGE_ROOT` 는 쓰지 않는다. MCP 는 저장하지 않는다.)
- `MARKET_REPLAY_FILE`: 명시적인 replay 모드에서 사용할 기록 파일 경로.
- `REPORT_TEMPLATES_DIR`: 양식 JSON 폴더. 기본 `config/report_templates`.
- `REPORT_DEFAULT_TEMPLATE`: 양식 도구 없이 기존 3개 시장 도구만 쓰는 경우의 기본 양식 ID. 기본 `market-daily`.

사용자 인증은 현재 개발용 `X-User-Id`이고, 로그인 구현이 붙으면 검증된 `Principal.user_id`를 그대로 사용한다.
MCP에 도달한 요청자 ID는 신뢰된 플랫폼이 채운다는 계약이며, 공유 배포 시 두 프로세스 사이 MCP 인증도 연결해야 한다.

## 구현한 보호·검증

- 기준일 생략 시 MCP가 한국 시간 오늘을 정한다. 미래·잘못된 날짜는 거절한다.
- 조회 범위는 기준일 이전 400일이며, 반환 범위를 넘는 응답을 무시하고 최신값인 것처럼 쓰지 않는다.
- 값이 없는 계열은 ‘값 없음’, 전체 실패는 오류로 표시한다.
- Decimal로 증감·증감률을 계산하고 JPY 값을 다시 100배 변환하지 않는다.
- 직전 승인 가능 관측값보다 환율 3% 초과·금리 0.5%p 초과 변동은 보류한다. 보류 사실을 표시한다.
- 저장은 본인 소유의 고정된 스냅샷으로 만든다. 모델의 환율·표·경로를 입력받지 않는다.
- 요약 숫자가 확인한 값·날짜에 없거나 금지 표현/문장 수를 위반하면 요약을 제외한다.
  변동 수치의 명시적 부호도 검사하며, 요청일보다 오래된 관측값을 가지고 ‘그 이후 변동 없음’이라고 단정하면 제외한다.
  숫자 집합 검사이며 모든 문장의 의미·통화별 대응을 완전하게 검증하는 것은 아니다.
- 시장 도구(+선택적 날짜 도구)만 있는 전용 에이전트에서는 조회 → 요약 1회 → 저장 순서를 코드가 보장한다.
  양식 도구가 있으면 검색 → 전체 읽기가 먼저 실행된다. 최종 답변은 서버가 저장한 본문을 직접 반환해 모델이 표를 다시 쓰지 않게 한다.
- 메일 등 다른 도구를 함께 넣은 복합 에이전트는 기존 일반 하네스를 사용한다. MCP의 수치 검사·소유자 검사는 동일하게 적용된다.
- 읽기·저장은 기존 권한/감사 경로를 거친다. 연결 장애 뒤 저장을 자동 재시도하지 않는다.

## 아직 별도 작업인 부분

- 자동 예약 실행, Reports/Inbox 전용 화면, PostgreSQL 적재, 망분리 파일 반입 경로.
- 급변 보류 값의 관리자 확인·승인 화면.
- 실제 운영 시 문서 보관 정책·데이터 최신성 기준·API 호출 캐시 및 호출량 제한.
- 실제 양식 RAG/벡터 검색 저장소, 회사·부서별 양식 접근 권한, 원본 HWPX/DOCX 서식을 유지하는 파일 출력.
- 시장 리포트 양식(`config/report_templates`)과 업무 서류 양식(`config/document_forms`)의 파일 형식 통일 (문서 형식·내려받기는 이미 같다).

## 회사 양식으로 교체하기

테스트용 세 양식은 `config/report_templates`에 있다. 제목·항목 순서·고정 문구·표 위치는 양식 JSON만 수정하면 다음 검색부터 반영된다.
기존 보고서는 당시 양식 버전과 본문을 보관하므로 바뀌지 않는다. 상세 입력 위치와 변경 절차는 `config/report_templates/README.md`를 참고한다.
새 양식은 `sections`로 제목·문단·필드·표의 배치를 정의한다. 새 보고서는 구조화 `document` JSON으로 저장하고 화면/PDF/DOCX가 이를 직접 사용한다
(PDF·DOCX 는 서버가 저장된 문서로 만든다: `apps/api/src/services/document_render.py`).
새 보고서 응답은 `document` JSON과 일반 텍스트 `text`를 반환하며 Markdown으로 변환하지 않는다. 이전 저장 파일과 양식은 호환 읽기를 유지한다.
현재는 로컬 키워드 검색이며 실제 RAG 연결을 완료한 것은 아니다. 검색 후보의 ID·해시로 원본을 읽는 계약은 검색 저장소를 교체해도 유지한다.

이번 구조에서는 MCP 서버가 직접 외부 API에 접근한다. 전달 자료의 ‘수집기→망연계→내부 DB’ 구조를 그대로 이식한 것은 아니다.
실제 망분리 환경에서는 MCP 배치 위치와 내부 저장소/반입 어댑터를 별도로 연결한다.

## 테스트

```cmd
.venv\Scripts\python.exe -B -X utf8 -m pytest -p no:cacheprovider tests/test_market_tools.py tests/test_report_templates.py
```

실제 API 키 대신 모의 ECOS 응답과 전달 기록 파일을 사용한다.
