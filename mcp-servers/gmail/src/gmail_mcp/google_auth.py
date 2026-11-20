"""직원 개인 Gmail 계정 OAuth 인증.

- credentials.json : 플랫폼의 Google Cloud 프로젝트에서 받은 OAuth 클라이언트(데스크톱 앱) 파일. 모든 직원이 함께 쓴다.
- tokens/<user_id>.json : 직원이 `uv run gmail-mcp-auth --user <사번>` 으로 **자기 계정**에 로그인하면 생성

모두 저장소에 커밋하지 않는다. 위치는 GMAIL_CREDENTIALS_FILE / GMAIL_TOKENS_DIR 로 바꿀 수 있다.
MCP 서버·동기화 실행기는 브라우저를 띄우지 않는다(interactive=False). 토큰이 없으면 AccountNotConnected.

웹 화면이 생기면 같은 흐름을 웹 OAuth(로그인 후 리디렉트)로 바꾸고, 토큰은 암호화해 DB 에 보관한다.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path

from .config import Settings

# 스코프를 바꾸면 토큰을 지우고 다시 인증해야 한다.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",  # 메일 검색·읽기, 증분 동기화
    "https://www.googleapis.com/auth/gmail.send",  # 메일 발송
]
USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GmailAuthError(RuntimeError):
    pass


class AccountNotConnected(GmailAuthError):
    """이 직원은 아직 메일 계정을 연결하지 않았다."""


def token_path(settings: Settings, user_id: str) -> Path:
    if not USER_ID_RE.fullmatch(user_id or ""):  # 경로 조작 방지
        raise GmailAuthError(f"잘못된 사용자 ID: {user_id!r}")
    return settings.tokens_dir / f"{user_id}.json"


def has_token(settings: Settings, user_id: str) -> bool:
    try:
        return token_path(settings, user_id).exists()
    except GmailAuthError:
        return False


def load_credentials(settings: Settings, user_id: str, *, interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(settings, user_id)
    creds = Credentials.from_authorized_user_file(str(path), SCOPES) if path.exists() else None
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        if not settings.credentials_file.exists():
            raise GmailAuthError(
                f"{settings.credentials_file} 가 없습니다. mcp-servers/gmail/README.md 의 'Google Cloud 설정'을 먼저 진행하세요."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(settings.credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise AccountNotConnected(f"{user_id} 의 메일 계정이 연결되지 않았습니다 (gmail-mcp-auth --user {user_id})")

    _save_token(path, creds.to_json())
    return creds


def _save_token(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows 에서는 효과가 제한적
        os.chmod(path, 0o600)


def _arg(name: str) -> str | None:
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def setup_main() -> None:
    """직원이 자기 Google 계정으로 로그인해 토큰을 만든다.  uv run gmail-mcp-auth --user E1001 [--force]"""
    import asyncio

    from googleapiclient.discovery import build

    from .store import MailStore

    settings = Settings.from_env()
    user_id = _arg("--user")
    if not user_id:
        print("사용법: uv run gmail-mcp-auth --user <사번> [--force]")
        sys.exit(2)
    path = token_path(settings, user_id)
    if path.exists() and "--force" not in sys.argv:
        print(f"이미 연결돼 있습니다: {user_id}  (다시 인증하려면 --force)")
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    creds = load_credentials(settings, user_id, interactive=True)
    email = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()[
        "emailAddress"].lower()
    asyncio.run(MailStore(settings.db_path).set_account(user_id, email))
    print(f"연결 완료: {user_id} ↔ {email}")
    print("본인 계정이 맞는지 확인하세요. 아니라면 --force 로 다시 인증합니다.")
