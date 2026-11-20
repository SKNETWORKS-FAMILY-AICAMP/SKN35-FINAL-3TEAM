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


def _bool(value: str | None, default: bool) -> bool:
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    adapter: str = "google"  # google(직원 개인 Gmail) | fake(가상 메일함, 테스트·로컬 시연)
    credentials_file: Path = DEFAULT_SECRETS_DIR / "credentials.json"  # 플랫폼의 OAuth 클라이언트 (모든 직원 공용)
    tokens_dir: Path = DEFAULT_SECRETS_DIR / "tokens"  # 직원별 토큰 <user_id>.json
    db_path: Path = Path("data/db/mail.sqlite3")  # 메일 저장 관계형 DB (개발용 SQLite)
    host: str = "127.0.0.1"
    port: int = 8001  # FastAPI 플랫폼(8000)과 동시에 실행
    max_search_results: int = 20
    max_recipients: int = 10
    max_subject_chars: int = 200
    max_body_chars: int = 20_000
    max_read_body_chars: int = 5_000  # read_mail 이 메시지당 돌려주는 본문 길이 (LLM 컨텍스트 보호)
    allowed_recipient_domains: tuple[str, ...] = field(default_factory=tuple)  # 비우면 제한 없음
    max_message_bytes: int = 25 * 1024 * 1024  # 이보다 큰 메일은 too_large 로 기록
    sync_on_search: bool = True  # search_mail 전에 그 직원의 새 메일을 먼저 가져옴
    search_sync_max_pages: int = 2  # 검색 전 동기화는 최대 N페이지만 (툴 30초 제한 안에서)
    prune_max_ratio: float = 0.3  # 삭제 판정이 저장된 메일의 30%를 넘으면 반영하지 않고 멈춤
    prune_min_docs: int = 20  # 저장된 메일이 이보다 적으면 비율 가드를 적용하지 않음

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        return cls(
            adapter=e("GMAIL_ADAPTER", cls.adapter),
            credentials_file=Path(e("GMAIL_CREDENTIALS_FILE", str(cls.credentials_file))),
            tokens_dir=Path(e("GMAIL_TOKENS_DIR", str(cls.tokens_dir))),
            db_path=Path(e("GMAIL_DB_PATH", str(cls.db_path))),
            host=e("MCP_HOST", cls.host),
            port=int(e("MCP_PORT", str(cls.port))),
            max_search_results=int(e("GMAIL_MAX_SEARCH_RESULTS", str(cls.max_search_results))),
            max_recipients=int(e("GMAIL_MAX_RECIPIENTS", str(cls.max_recipients))),
            allowed_recipient_domains=_split(e("GMAIL_ALLOWED_RECIPIENT_DOMAINS")),
            max_message_bytes=int(e("GMAIL_MAX_MESSAGE_BYTES", str(cls.max_message_bytes))),
            sync_on_search=_bool(e("GMAIL_SYNC_ON_SEARCH"), cls.sync_on_search),
            search_sync_max_pages=int(e("GMAIL_SEARCH_SYNC_MAX_PAGES", str(cls.search_sync_max_pages))),
            prune_max_ratio=float(e("GMAIL_PRUNE_MAX_RATIO", str(cls.prune_max_ratio))),
            prune_min_docs=int(e("GMAIL_PRUNE_MIN_DOCS", str(cls.prune_min_docs))),
        )
