"""증분 동기화 · 슬림 문서 목록 (팀 Gmail 메일함 → MailDocument).

설계 참고 (Onyx, MIT 라이선스 부분)
- Gmail 커넥터: `after:<epoch> before:<epoch>` 시간 범위 검색, 체크포인트 1회 = 목록 1페이지,
  문서 단위 실패는 ConnectorFailure 로 기록하고 계속, 슬림 목록의 페이지 토큰 반복 · 최대 페이지 가드
- Confluence 커넥터: 마지막 동기화 시각 이후만 가져오는 시간 창(window), 다음 페이지 커서를 체크포인트에 저장해
  중단돼도 이어서 진행, 시간 경계 누락을 막는 여유 구간(Confluence 는 end +1일, 여기서는 start -overlap)
코드는 이 프로젝트에 맞게 새로 작성했다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from .client import LIST_PAGE_SIZE, GmailClient
from .convert import doc_id_for, thread_link, thread_to_document, thread_to_slim
from .models import MailCheckpoint, MailDocument, SlimMailDocument, SyncFailure

# 경계 근처에 도착한 메일·시계 오차로 누락되지 않도록 시작 시각을 앞당기는 여유 구간.
# 겹쳐서 다시 가져온 스레드는 doc_id 가 같아 덮어쓰기(멱등)이므로 안전하다.
DEFAULT_OVERLAP_SECONDS = 10 * 60
SLIM_BATCH_SIZE = 500
MAX_SLIM_PAGES = 10_000


def build_time_range_query(start: float | None, end: float | None) -> str | None:
    """Gmail 검색어. 예: 'after:1700000000 before:1710000000'. 둘 다 없으면 None (전체)."""
    parts = []
    if start:
        parts.append(f"after:{int(start)}")
    if end:
        parts.append(f"before:{int(end)}")
    return " ".join(parts) or None


class GmailSyncConnector:
    def __init__(
        self,
        client: GmailClient,
        mailbox_owner: str,
        *,
        page_size: int = LIST_PAGE_SIZE,
        overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
    ) -> None:
        self.client = client
        self.mailbox_owner = mailbox_owner.lower()
        self.page_size = page_size
        self.overlap_seconds = overlap_seconds

    def window_query(self, start: float | None, end: float | None) -> str | None:
        effective_start = max(0.0, start - self.overlap_seconds) if start else None
        return build_time_range_query(effective_start, end)

    async def load_from_checkpoint(
        self, start: float | None, end: float | None, checkpoint: MailCheckpoint
    ) -> tuple[list[MailDocument | SyncFailure], MailCheckpoint]:
        """목록 1페이지를 처리하고 다음 체크포인트를 돌려준다.

        호출하는 쪽은 checkpoint.has_more 가 False 가 될 때까지 반복하고,
        매 페이지 뒤에 체크포인트를 저장해 두면 중간에 멈춰도 그 페이지부터 다시 시작할 수 있다.
        """
        if not checkpoint.has_more:
            return [], checkpoint
        thread_ids, next_token = await self.client.list_threads(
            self.window_query(start, end), checkpoint.page_token, self.page_size
        )
        results: list[MailDocument | SyncFailure] = []
        for thread_id in thread_ids:
            results.append(await self._load_one(thread_id))
        results = [r for r in results if r is not None]
        return results, MailCheckpoint(has_more=next_token is not None, page_token=next_token)

    async def _load_one(self, thread_id: str) -> MailDocument | SyncFailure | None:
        try:
            thread = await self.client.get_thread(thread_id)
            if thread is None:
                return SyncFailure(doc_id=doc_id_for(thread_id), link=thread_link(thread_id),
                                   error="목록 조회 후 스레드가 사라짐 (삭제됨)")
            return thread_to_document(thread, self.mailbox_owner)  # 메시지 없는 스레드는 None
        except Exception as exc:  # 문서 하나의 실패가 전체 동기화를 멈추지 않게
            return SyncFailure(doc_id=doc_id_for(thread_id), link=thread_link(thread_id),
                               error=f"{type(exc).__name__}: {exc}")

    async def retrieve_all_slim_docs_perm_sync(
        self,
        start: float | None = None,
        end: float | None = None,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> AsyncIterator[list[SlimMailDocument]]:
        """본문 없이 ID + 권한만 배치로 돌려준다 (권한 동기화 · 삭제 감지용).

        목록이 불완전한 상태로 끝나면 삭제 감지가 멀쩡한 문서를 지울 수 있으므로,
        페이지 토큰이 반복되거나 최대 페이지를 넘으면 부분 결과로 끝내지 않고 예외를 낸다.
        """
        query = build_time_range_query(start, end)
        token: str | None = None
        seen_tokens: set[str] = set()
        batch: list[SlimMailDocument] = []
        for _ in range(MAX_SLIM_PAGES):
            if should_stop and should_stop():
                raise RuntimeError("gmail 슬림 목록: 중단 신호")
            thread_ids, token = await self.client.list_threads(query, token, self.page_size)
            for thread_id in thread_ids:
                thread = await self.client.get_thread(thread_id, metadata_only=True)
                slim = thread_to_slim(thread, self.mailbox_owner) if thread else None
                if slim:
                    batch.append(slim)
                if len(batch) >= SLIM_BATCH_SIZE:
                    yield batch
                    batch = []
            if token is None:
                break
            if token in seen_tokens:
                raise RuntimeError("gmail 슬림 목록: 페이지 토큰 반복 — 목록이 불완전할 수 있어 중단")
            seen_tokens.add(token)
        else:
            raise RuntimeError(f"gmail 슬림 목록: 최대 {MAX_SLIM_PAGES}페이지 초과 — 목록이 불완전할 수 있어 중단")
        if batch:
            yield batch
