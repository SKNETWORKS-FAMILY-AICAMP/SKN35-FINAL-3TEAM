"""Gmail API 접근 계층. 실제(GoogleGmailClient)와 가상 메일함(FakeGmailClient)을 바꿔 끼운다.

google-api-python-client 는 동기(블로킹) 라이브러리라서 asyncio.to_thread 로 감싼다 (NFR-04).
httplib2 가 스레드 안전하지 않으므로 호출마다 service 객체를 새로 만든다.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Protocol

from .config import Settings

LIST_PAGE_SIZE = 100
METADATA_HEADERS = ["From", "To", "Cc", "Bcc", "Subject", "Date"]
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GmailClient(Protocol):
    async def get_profile_email(self) -> str: ...

    async def list_threads(
        self, query: str | None, page_token: str | None, max_results: int = LIST_PAGE_SIZE
    ) -> tuple[list[str], str | None]:
        """(스레드 ID 목록, 다음 페이지 토큰). 스팸·휴지통은 제외된다(Gmail 기본값)."""
        ...

    async def get_thread(self, thread_id: str, *, metadata_only: bool = False) -> dict[str, Any] | None:
        """스레드 원본(dict). 없으면 None. metadata_only 면 헤더만 (권한 동기화용)."""
        ...

    async def send_message(self, raw: str) -> dict[str, Any]:
        """raw: base64url 인코딩된 RFC 2822 메시지."""
        ...


class GoogleGmailClient:
    """팀 Gmail 계정(userId="me")에 접근한다."""

    def __init__(self, settings: Settings, max_retries: int = 3) -> None:
        self.settings = settings
        self.max_retries = max_retries

    def _service(self):
        from googleapiclient.discovery import build

        from .google_auth import load_credentials

        return build("gmail", "v1", credentials=load_credentials(self.settings), cache_discovery=False)

    async def _call(self, fn):
        """429·5xx 는 지수 백오프로 재시도 (Onyx Gmail 커넥터의 재시도 정책을 참고)."""
        from googleapiclient.errors import HttpError

        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.to_thread(fn)
            except HttpError as exc:
                if exc.resp.status not in RETRYABLE_STATUS or attempt == self.max_retries:
                    raise
                await asyncio.sleep(min(2**attempt + random.random(), 30))

    async def get_profile_email(self) -> str:
        profile = await self._call(lambda: self._service().users().getProfile(userId="me").execute())
        return profile["emailAddress"].lower()

    async def list_threads(self, query, page_token, max_results=LIST_PAGE_SIZE):
        def run():
            kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_results}
            if query:
                kwargs["q"] = query
            if page_token:
                kwargs["pageToken"] = page_token
            return self._service().users().threads().list(**kwargs).execute()

        res = await self._call(run)
        return [t["id"] for t in res.get("threads", [])], res.get("nextPageToken")

    async def get_thread(self, thread_id, *, metadata_only=False):
        from googleapiclient.errors import HttpError

        def run():
            threads = self._service().users().threads()
            if metadata_only:
                return threads.get(userId="me", id=thread_id, format="metadata", metadataHeaders=METADATA_HEADERS).execute()
            return threads.get(userId="me", id=thread_id, format="full").execute()

        try:
            return await self._call(run)
        except HttpError as exc:
            if exc.resp.status == 404:  # 목록 조회와 상세 조회 사이에 삭제된 경우
                return None
            raise

    async def send_message(self, raw):
        return await self._call(lambda: self._service().users().messages().send(userId="me", body={"raw": raw}).execute())


def build_client(settings: Settings) -> GmailClient:
    if settings.adapter == "google":
        return GoogleGmailClient(settings)
    if settings.adapter == "fake":
        from .fake import FakeGmailClient, sample_mailbox

        return sample_mailbox(FakeGmailClient())
    raise RuntimeError(f"알 수 없는 어댑터: {settings.adapter}")
