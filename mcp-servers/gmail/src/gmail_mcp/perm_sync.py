"""권한 동기화 · 삭제 감지.

설계 참고: Onyx 권한 동기화의 흐름(슬림 문서를 받아 문서별 접근 권한을 내보냄, 중단 신호 확인).
해당 Onyx 코드(backend/ee/...)는 Onyx Enterprise License 이므로 코드는 사용하지 않았고,
흐름만 참고해 이 프로젝트용으로 새로 작성했다.

왜 따로 필요한가
- 증분 동기화는 "새로 바뀐 메일"만 가져온다. 이미 가져온 메일의 열람 권한이 바뀌거나(참여자 매핑 변경),
  원본에서 삭제된 경우는 증분 동기화로 알 수 없다 → 전체 슬림 목록으로 주기적으로 다시 맞춘다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable

from .models import DocAccessUpdate
from .sync import GmailSyncConnector


async def gmail_doc_sync(
    connector: GmailSyncConnector,
    *,
    since: float | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AsyncIterator[DocAccessUpdate]:
    """문서별 최신 접근 권한을 내보낸다. since 를 주면 그 이후 메일만 (증분 권한 동기화)."""
    async for batch in connector.retrieve_all_slim_docs_perm_sync(since, None, should_stop=should_stop):
        for slim in batch:
            if should_stop and should_stop():
                raise RuntimeError("gmail_doc_sync: 중단 신호")
            yield DocAccessUpdate(doc_id=slim.doc_id, access=slim.access)


async def find_deleted_docs(connector: GmailSyncConnector, existing_doc_ids: Iterable[str]) -> set[str]:
    """색인에는 있지만 메일함에서 사라진 문서 ID.

    전체 목록(시간 제한 없음)을 끝까지 받은 경우에만 결과를 낸다.
    목록이 불완전하면 retrieve_all_slim_docs_perm_sync 가 예외를 내므로 잘못 삭제하지 않는다.
    """
    live: set[str] = set()
    async for batch in connector.retrieve_all_slim_docs_perm_sync(None, None):
        live.update(s.doc_id for s in batch)
    return {d for d in existing_doc_ids if d.startswith("gmail:")} - live
