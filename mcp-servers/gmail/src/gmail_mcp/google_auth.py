"""팀 Gmail 계정 OAuth 인증.

- credentials.json : 팀 계정의 Google Cloud 프로젝트에서 받은 OAuth 클라이언트(데스크톱 앱) 파일
- token.json       : `uv run gmail-mcp-auth` 를 팀 계정으로 한 번 실행하면 생성 (리프레시 토큰 포함)

두 파일 모두 저장소에 커밋하지 않는다. 경로는 GMAIL_CREDENTIALS_FILE / GMAIL_TOKEN_FILE 로 바꿀 수 있다.
MCP 서버·동기화 실행기는 브라우저를 띄우지 않는다(interactive=False). 토큰이 없으면 안내 오류를 낸다.
"""

from __future__ import annotations

import contextlib
import os
import sys

from .config import Settings

# 스코프를 바꾸면 token.json 을 지우고 gmail-mcp-auth 를 다시 실행해야 한다.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",  # 메일 검색·읽기, 증분 동기화
    "https://www.googleapis.com/auth/gmail.send",  # 메일 발송
]


class GmailAuthError(RuntimeError):
    pass


def load_credentials(settings: Settings, *, interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if settings.token_file.exists():
        creds = Credentials.from_authorized_user_file(str(settings.token_file), SCOPES)
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        if not settings.credentials_file.exists():
            raise GmailAuthError(
                f"{settings.credentials_file} 가 없습니다. mcp-servers/gmail/README.md 의 '팀 계정 설정'을 먼저 진행하세요."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(settings.credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise GmailAuthError("Gmail 인증 토큰이 없습니다. 팀 계정으로 `uv run gmail-mcp-auth` 를 먼저 실행하세요.")

    _save_token(settings, creds.to_json())
    return creds


def _save_token(settings: Settings, content: str) -> None:
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(content, encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows 에서는 효과가 제한적
        os.chmod(settings.token_file, 0o600)


def setup_main() -> None:
    """최초 1회: 브라우저에서 **팀 Google 계정**으로 로그인해 token.json 을 만든다."""
    settings = Settings.from_env()
    if settings.token_file.exists() and "--force" not in sys.argv:
        print(f"이미 토큰이 있습니다: {settings.token_file}  (다시 인증하려면 --force)")
        return
    if "--force" in sys.argv:
        with contextlib.suppress(FileNotFoundError):
            settings.token_file.unlink()
    creds = load_credentials(settings, interactive=True)

    from googleapiclient.discovery import build

    profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
    print(f"인증 완료: {profile.get('emailAddress')}  → 토큰 저장 위치 {settings.token_file}")
    print("팀 계정이 맞는지 확인하세요. 아니라면 --force 로 다시 인증합니다.")
