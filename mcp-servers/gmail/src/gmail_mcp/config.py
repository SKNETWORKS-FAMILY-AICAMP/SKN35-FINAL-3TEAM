"""환경변수 설정 (.env). 비밀값·토큰 내용은 로그에 출력하지 않는다 (NFR-03)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# mcp-servers/gmail/ — 개발 환경 기본 비밀 파일 위치 (.secrets/ 는 .gitignore)
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SECRETS_DIR = PACKAGE_ROOT / ".secrets"


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in (value or "").split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    adapter: str = "google"  # google(팀 Gmail 계정) | fake(가상 메일함, 테스트·로컬 시연)
    credentials_file: Path = DEFAULT_SECRETS_DIR / "credentials.json"  # 팀 계정 OAuth 클라이언트
    token_file: Path = DEFAULT_SECRETS_DIR / "token.json"  # gmail-mcp-auth 실행 후 생성
    host: str = "127.0.0.1"
    port: int = 8000
    max_search_results: int = 20
    max_recipients: int = 10
    max_subject_chars: int = 200
    max_body_chars: int = 20_000
    max_read_body_chars: int = 5_000  # read_mail 이 메시지당 돌려주는 본문 길이 (LLM 컨텍스트 보호)
    allowed_recipient_domains: tuple[str, ...] = field(default_factory=tuple)  # 비우면 제한 없음
    sync_dir: Path = Path("data/sync/gmail")

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        return cls(
            adapter=e("GMAIL_ADAPTER", cls.adapter),
            credentials_file=Path(e("GMAIL_CREDENTIALS_FILE", str(cls.credentials_file))),
            token_file=Path(e("GMAIL_TOKEN_FILE", str(cls.token_file))),
            host=e("MCP_HOST", cls.host),
            port=int(e("MCP_PORT", str(cls.port))),
            max_search_results=int(e("GMAIL_MAX_SEARCH_RESULTS", str(cls.max_search_results))),
            max_recipients=int(e("GMAIL_MAX_RECIPIENTS", str(cls.max_recipients))),
            allowed_recipient_domains=_split(e("GMAIL_ALLOWED_RECIPIENT_DOMAINS")),
            sync_dir=Path(e("GMAIL_SYNC_DIR", str(cls.sync_dir))),
        )
