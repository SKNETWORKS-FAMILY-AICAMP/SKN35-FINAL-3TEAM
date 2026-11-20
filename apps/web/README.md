# Agent Workspace (프론트엔드)

React + Vite 화면입니다. FastAPI 백엔드(`apps/api`)에 연결되어 에이전트 만들기·관리·공유와 채팅을 합니다.

## 실행

Node.js 20.19 이상이 필요합니다. 터미널 두 개에서 저장소 루트 기준으로 실행합니다.

1. 백엔드 (Ollama 에 `qwen3:8b` 가 있어야 합니다. LLM 없이 해 보려면 앞에 `LLM_PROVIDER=local_math` 를 붙입니다)

   ```bash
   uv run python -m uvicorn main:app --app-dir apps/api/src --reload
   ```

2. 프론트

   ```bash
   cd apps/web
   npm ci
   npm run dev
   ```

터미널에 나온 주소(기본 http://localhost:5173)를 엽니다. `/api` 요청은 `vite.config.js` 의 프록시가 http://localhost:8000 으로 넘깁니다.
백엔드 주소가 다르면 `VITE_API_TARGET=http://localhost:8001 npm run dev` 로 바꿉니다.

## 사용법

- 왼쪽 아래에서 **사용자**(kim·lee·park·admin)를 고릅니다. 로그인이 붙기 전까지 쓰는 개발용 선택입니다 (`config/dev_users.yaml`).
- **에이전트 만들기**: 필요한 역할을 자연어로 쓰고 **초안 생성**을 누르면 LLM 이 이름·프롬프트·도구를 골라 초안을 만듭니다 (20~40초). 확인 후 저장하면 개인용으로 만들어집니다.
- **에이전트 관리**: 내가 만든 것과 공유받은 것을 봅니다. 부서·전사 공유 요청, 공유 해제, 비활성화, 삭제를 합니다. 공유 중이거나 승인 대기 중인 에이전트는 수정할 수 없습니다.
- **공유 요청**: 바로 위 관리자(팀원 → 부서장, 부서장 → admin)가 받은 요청을 승인·반려합니다.
- **작업 채팅**: 에이전트를 골라 대화합니다. 실행한 도구와 결과, 권한 거부가 답 위에 표시되고, 승인이 필요한 도구는 채팅 안에서 승인·거절합니다.
- 서버를 다시 켜면 에이전트와 대화가 사라집니다 (아직 DB 미연결).
