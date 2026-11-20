"""증분 동기화 · 메일 ID 전체 목록 — 직원 한 명의 메일함 → 관계형 DB.

설계 참고 (Onyx, MIT 라이선스 부분)
- Gmail 커넥터: `after:<epoch> before:<epoch>` 시간 범위 검색, 체크포인트 1회 = 목록 1페이지,
  문서 단위 실패는 ConnectorFailure 로 기록하고 계속, 슬림 목록(ID 만)의 페이지 토큰 반복 · 최대 페이지 가드
- Confluence 커넥터: 마지막 동기화 시각 이후만 가져오는 시간 창(window), 다음 페이지 커서를 체크포인트에 저장해
  중단돼도 이어서 진행, 시간 경계 누락을 막는 여유 구간(Confluence 는 end +1일, 여기서는 start -overlap)
코드는 이 프로젝트에 맞게 새로 작성했다.

개인 메일함이므로 권한은 "메일함 주인 한 명"으로 고정이다 (Onyx 와 같은 전제).
그래서 권한을 다시 계산하는 권한 동기화는 필요 없고, 삭제 감지에는 메일 ID 목록만 있으면 된다.
"""

from __future__ import annotations

from .client import LIST_PAGE_SIZE, GmailClient
from .convert import message_from_api, to_record
from .models import DeletedCandidate, FailureReason, MailCheckpoint, MailRecord, SyncFailure

DEFAULT_OVERLAP_SECONDS = 10 * 60  # 경계 근처 메일 누락 방지. 다시 본 메일은 해시가 같아 DB 를 쓰지 않는다
DEFAULT_MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_LIST_PAGES = 10_000

SyncResult = MailRecord | SyncFailure | DeletedCandidate


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
        owner_user_id: str,
        *,
        page_size: int = LIST_PAGE_SIZE,
        overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        self.client = client
        self.owner_user_id = owner_user_id
        self.page_size = page_size
        self.overlap_seconds = overlap_seconds
        self.max_message_bytes = max_message_bytes

    def window_query(self, start: float | None, end: float | None) -> str | None:
        effective_start = max(0.0, start - self.overlap_seconds) if start else None
        return build_time_range_query(effective_start, end)

    async def load_from_checkpoint(
        self, start: float | None, end: float | None, checkpoint: MailCheckpoint
    ) -> tuple[list[SyncResult], MailCheckpoint]:
        """목록 1페이지를 처리하고 다음 체크포인트를 돌려준다. 매 페이지 뒤에 체크포인트를 저장하면 이어서 진행할 수 있다."""
        if not checkpoint.has_more:
            return [], checkpoint
        message_ids, next_token = await self.client.list_messages(
            self.window_query(start, end), checkpoint.page_token, self.page_size
        )
        results = [await self.fetch(message_id) for message_id in message_ids]
        return results, MailCheckpoint(has_more=next_token is not None, page_token=next_token)

    async def fetch(self, message_id: str) -> SyncResult:
        try:
            msg = await self.client.get_message(message_id, format="full")
        except Exception as exc:  # 한 통의 실패가 전체 동기화를 멈추지 않게
            return SyncFailure(message_id=message_id, reason=FailureReason.FETCH_ERROR,
                               detail=f"{type(exc).__name__}: {exc}")
        if msg is None:
            return DeletedCandidate(message_id=message_id)
        if not msg.get("id"):
            return SyncFailure(message_id=message_id, reason=FailureReason.INVALID_MESSAGE, detail="메시지 ID 없음")
        size = int(msg.get("sizeEstimate") or 0)
        if size > self.max_message_bytes:  # 건너뛰지 않고 사유를 남긴다
            return SyncFailure(message_id=message_id, reason=FailureReason.TOO_LARGE,
                               detail=f"{size} bytes > {self.max_message_bytes}")
        return to_record(message_from_api(msg), self.owner_user_id, size)

    async def list_all_message_ids(self) -> set[str]:
        """메일함의 메일 ID 전체 (삭제 감지용 슬림 목록).

        목록이 불완전한 상태로 끝나면 멀쩡한 메일을 지울 수 있으므로,
        페이지 토큰이 반복되거나 최대 페이지를 넘으면 부분 결과로 끝내지 않고 예외를 낸다.
        """
        ids: set[str] = set()
        token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            page, token = await self.client.list_messages(None, token, self.page_size)
            ids.update(page)
            if token is None:
                return ids
            if token in seen_tokens:
                raise RuntimeError("gmail 메일 목록: 페이지 토큰 반복 — 목록이 불완전할 수 있어 중단")
            seen_tokens.add(token)
        raise RuntimeError(f"gmail 메일 목록: 최대 {MAX_LIST_PAGES}페이지 초과 — 목록이 불완전할 수 있어 중단")
