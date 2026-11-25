"""동기화 실행기 — 직원별 메일함을 관계형 DB 로 가져온다. 스케줄러(cron 등)가 주기적으로 실행한다.

  uv run gmail-sync --user E1001                 # 증분 동기화 (마지막 성공 이후 메일)
  uv run gmail-sync --all                        # 계정을 연결한 모든 직원
  uv run gmail-sync --user E1001 --full          # 처음부터 전체 다시 확인 (바뀐 것만 DB 에 씀)
  uv run gmail-sync --user E1001 --prune         # 삭제 감지 (원본에서 사라진 메일을 DB 에서 삭제)
  uv run gmail-sync --user E1001 --prune --force # 삭제 비율 가드를 넘어도 반영 (사람이 확인한 뒤에만)

모든 결과는 DB 에 남는다: mail_message(메일) · mail_sync_state(재개 지점) · mail_sync_failure(사유 코드) ·
mail_sync_run(실행마다 건수). 벡터DB · 청킹에는 넣지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

from .client import GmailClient
from .config import Settings
from .models import ChangeKind, DeletedCandidate, MailCheckpoint, SyncFailure
from .store import MailStore, open_store
from .sync import GmailSyncConnector


class PruneGuardError(RuntimeError):
    """삭제 판정이 너무 많아 반영하지 않고 멈췄다 (목록 오류 · 계정 착오 방지)."""


def new_job_id(prefix: str) -> str:
    """같은 초에 여러 번 실행돼도 섞이지 않도록 무작위 접미사를 붙인다."""
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


@dataclass
class SyncStats:
    owner_user_id: str
    job_id: str
    mode: str
    pages: int = 0
    resumed: bool = False
    completed: bool = True  # 페이지 제한으로 중간에 멈췄으면 False (다음 실행이 이어서 진행)
    changes: Counter = field(default_factory=Counter)  # new / updated / deleted
    unchanged: int = 0
    failures: Counter = field(default_factory=Counter)  # 사유 코드별
    skipped: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {"owner_user_id": self.owner_user_id, "job_id": self.job_id, "mode": self.mode, "pages": self.pages,
                "resumed": self.resumed, "completed": self.completed, "changes": dict(self.changes),
                "unchanged": self.unchanged, "failures": dict(self.failures), "skipped": dict(self.skipped)}


async def run_incremental(
    connector: GmailSyncConnector,
    store: MailStore,
    *,
    now: float | None = None,
    full: bool = False,
    max_pages: int | None = None,
) -> SyncStats:
    owner = connector.owner_user_id
    state = await store.get_sync_state(owner)
    resumed = state.in_progress and not full
    if resumed:
        job_id, start, end = state.job_id or new_job_id("gmail"), state.window_start, state.window_end
        checkpoint = MailCheckpoint(has_more=state.has_more, page_token=state.page_token)
    else:
        job_id, start, end = new_job_id("gmail"), (None if full else state.last_sync_end), (now or time.time())
        checkpoint = MailCheckpoint()
        await store.save_sync_state(owner, last_sync_end=state.last_sync_end, window_start=start, window_end=end,
                                    checkpoint=checkpoint, job_id=job_id)

    stats = SyncStats(owner_user_id=owner, job_id=job_id, mode="full" if full else "incremental", resumed=resumed)
    while checkpoint.has_more:
        if max_pages is not None and stats.pages >= max_pages:
            stats.completed = False
            break
        results, checkpoint = await connector.load_from_checkpoint(start, end, checkpoint)
        failures: list[SyncFailure] = []
        deleted: list[str] = []
        for r in results:
            if isinstance(r, SyncFailure):
                failures.append(r)
                stats.failures[str(r.reason)] += 1
            elif isinstance(r, DeletedCandidate):  # 실패가 아니라 삭제 후보
                deleted.append(r.message_id)
            else:
                kind = await store.upsert(r)  # 같으면 쓰지 않음
                if kind is None:
                    stats.unchanged += 1
                else:
                    stats.changes[str(kind)] += 1
        if deleted:
            removed = await store.delete(owner, deleted)
            stats.changes[str(ChangeKind.DELETED)] += removed
            stats.skipped["deleted_during_fetch"] += len(deleted) - removed
        await store.record_failures(owner, job_id, failures)
        stats.pages += 1
        await store.save_sync_state(owner, last_sync_end=state.last_sync_end, window_start=start, window_end=end,
                                    checkpoint=checkpoint, job_id=job_id)

    if stats.completed:
        await store.save_sync_state(owner, last_sync_end=end, window_start=None, window_end=None,
                                    checkpoint=None, job_id=None)
    await store.record_run(owner, job_id, stats.mode, stats.as_dict())
    return stats


async def run_prune(
    connector: GmailSyncConnector, store: MailStore, *, max_ratio: float = 0.3, min_docs: int = 20, force: bool = False
) -> SyncStats:
    """원본에서 사라진 메일을 DB 에서 지운다.

    - 메일 목록이 불완전하면(페이지 토큰 반복 등) 예외 → 아무것도 지우지 않음
    - 삭제 판정이 저장된 메일의 max_ratio 를 넘으면 반영하지 않고 PruneGuardError (계정 착오 · API 오류 방지)
    """
    owner = connector.owner_user_id
    stats = SyncStats(owner_user_id=owner, job_id=new_job_id("gmail-prune"), mode="prune")
    stored = await store.message_ids(owner)
    deleted = sorted(stored - await connector.list_all_message_ids())
    total = len(stored)
    if not force and total >= min_docs and deleted and len(deleted) / total > max_ratio:
        stats.skipped["prune_guard"] = len(deleted)
        await store.record_run(owner, stats.job_id, stats.mode, stats.as_dict())
        raise PruneGuardError(
            f"삭제 판정 {len(deleted)}건 / 저장 {total}건 ({len(deleted) / total:.0%}) — "
            f"기준 {max_ratio:.0%} 초과로 반영하지 않았습니다. 확인 후 --prune --force 로 실행하세요."
        )
    stats.changes[str(ChangeKind.DELETED)] = await store.delete(owner, deleted)
    stats.unchanged = total - len(deleted)
    await store.record_run(owner, stats.job_id, stats.mode, stats.as_dict())
    return stats


# ── CLI ───────────────────────────────────────────────────


def _arg(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


async def _sync_user(settings: Settings, store: MailStore, user_id: str, argv: list[str]) -> dict:
    from .accounts import client_for

    client: GmailClient = client_for(settings, user_id)
    connector = GmailSyncConnector(client, user_id, max_message_bytes=settings.max_message_bytes)
    try:
        if "--prune" in argv:
            stats = await run_prune(connector, store, max_ratio=settings.prune_max_ratio,
                                    min_docs=settings.prune_min_docs, force="--force" in argv)
        else:
            stats = await run_incremental(connector, store, full="--full" in argv)
    except PruneGuardError as exc:
        return {"owner_user_id": user_id, "error": "prune_guard", "message": str(exc)}
    return stats.as_dict()


async def _main(argv: list[str]) -> int:
    settings = Settings.from_env()
    store = open_store(settings)
    try:
        return await _run_users(settings, store, argv)
    finally:
        await store.close()


async def _run_users(settings: Settings, store: MailStore, argv: list[str]) -> int:
    if "--all" in argv:
        users = await store.connected_users()
    elif user := _arg(argv, "--user"):
        users = [user]
    else:
        print("사용법: uv run gmail-sync --user <사번> [--full | --prune [--force]]  또는  --all")
        return 2
    code = 0
    for user_id in users:
        result = await _sync_user(settings, store, user_id, argv)
        code = code or (2 if "error" in result else 0)
        print(json.dumps(result, ensure_ascii=False))
    return code


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
