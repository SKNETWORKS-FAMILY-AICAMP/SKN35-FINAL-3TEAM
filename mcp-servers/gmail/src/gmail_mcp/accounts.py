"""직원 ↔ 메일함 연결. 요청한 직원의 메일함에만 붙는 클라이언트를 만든다."""

from __future__ import annotations

from collections.abc import Callable

from .client import GmailClient, GoogleGmailClient
from .config import Settings
from .google_auth import AccountNotConnected, has_token

ClientFactory = Callable[[str], GmailClient]

_fake_boxes: dict[str, GmailClient] | None = None


def client_for(settings: Settings, user_id: str) -> GmailClient:
    """user_id 직원의 메일함 클라이언트. 계정이 연결되지 않았으면 AccountNotConnected."""
    if settings.adapter == "fake":
        global _fake_boxes
        if _fake_boxes is None:
            from .fake import sample_mailboxes

            _fake_boxes = sample_mailboxes()
        if user_id not in _fake_boxes:
            raise AccountNotConnected(f"{user_id} 의 메일 계정이 연결되지 않았습니다 (가상 메일함에 없는 사번)")
        return _fake_boxes[user_id]
    if settings.adapter == "google":
        if not has_token(settings, user_id):
            raise AccountNotConnected(f"{user_id} 의 메일 계정이 연결되지 않았습니다 (gmail-mcp-auth --user {user_id})")
        return GoogleGmailClient(settings, user_id)
    raise RuntimeError(f"알 수 없는 어댑터: {settings.adapter}")


def factory(settings: Settings) -> ClientFactory:
    return lambda user_id: client_for(settings, user_id)
