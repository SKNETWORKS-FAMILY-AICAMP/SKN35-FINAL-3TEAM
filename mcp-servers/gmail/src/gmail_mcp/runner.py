"""동기화 실행기 — 스케줄러(cron 등)가 주기적으로 실행한다. LLM 툴이 아니다.

  uv run gmail-sync                 # 증분 동기화 (마지막 성공 시각 이후)
  uv run gmail-sync --full          # 처음부터 전체 동기화
  uv run gmail-sync --perm-sync     # 권한 동기화 (전체 슬림 목록으로 권한 다시 계산)

결과 (GMAIL_SYNC_DIR, 기본 data/sync/gmail/)
  state.json                         마지막 성공 시각 + 진행 중 체크포인트 (중단 시 이어서)
  documents/<job_id>.jsonl           MailDocument (문서 처리 파트가 파싱·청킹)
  failures/<job_id>.jsonl            문서 단위 실패
  permissions/<job_id>.jsonl         DocAccessUpdate (권한 동기화 결과)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from .client import GmailClient, build_client
from .config import Settings
from .models import MailCheckpoint, MailDocument, SyncFailure
from .perm_sync import gmail_doc_sync
from .sync import GmailSyncConnector


class SyncState(BaseModel):
    last_sync_end: float | None = None  # 마지막으로 끝까지 성공한 창의 끝 시각 (다음 창의 시작)
    window_start: float | None = None  # 진행 중인 창 (중단됐을 때만 값이 있음)
    window_end: float | None = None
    checkpoint: MailCheckpoint | None = None
    job_id: str | None = None


def load_state(path: Path) -> SyncState:
    return SyncState.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else SyncState()


def save_state(path: Path, state: SyncState) -> None:
    """임시 파일에 쓴 뒤 교체 — 저장 도중 멈춰도 state.json 이 깨지지 않게."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def new_job_id(prefix: str) -> str:
    """같은 초에 여러 번 실행돼도 결과 파일이 섞이지 않도록 무작위 접미사를 붙인다."""
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _append(path: Path, items: list[BaseModel]) -> None:
    if not items:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for item in items:
            f.write(item.model_dump_json() + "\n")


@dataclass
class SyncStats:
    job_id: str
    documents: int = 0
    failures: int = 0
    pages: int = 0
    resumed: bool = False


async def run_incremental(
    connector: GmailSyncConnector, sync_dir: Path, *, now: float | None = None, full: bool = False
) -> SyncStats:
    state_path = sync_dir / "state.json"
    state = load_state(state_path)
    resumed = state.checkpoint is not None and not full

    if not resumed:
        state = SyncState(
            last_sync_end=None if full else state.last_sync_end,
            window_start=None if full else state.last_sync_end,
            window_end=now or time.time(),
            checkpoint=MailCheckpoint(),
            job_id=new_job_id("gmail"),
        )
        save_state(state_path, state)

    stats = SyncStats(job_id=state.job_id or "gmail", resumed=resumed)
    checkpoint = state.checkpoint or MailCheckpoint()
    while checkpoint.has_more:
        results, checkpoint = await connector.load_from_checkpoint(state.window_start, state.window_end, checkpoint)
        docs = [r for r in results if isinstance(r, MailDocument)]
        failures = [r for r in results if isinstance(r, SyncFailure)]
        _append(sync_dir / "documents" / f"{stats.job_id}.jsonl", docs)
        _append(sync_dir / "failures" / f"{stats.job_id}.jsonl", failures)
        stats.documents += len(docs)
        stats.failures += len(failures)
        stats.pages += 1
        state.checkpoint = checkpoint
        save_state(state_path, state)  # 페이지마다 저장 → 중단돼도 이 페이지 다음부터

    save_state(state_path, SyncState(last_sync_end=state.window_end))
    return stats


async def run_perm_sync(connector: GmailSyncConnector, sync_dir: Path) -> int:
    job_id = new_job_id("gmail-perm")
    out = sync_dir / "permissions" / f"{job_id}.jsonl"
    batch, count = [], 0
    async for update in gmail_doc_sync(connector):
        batch.append(update)
        if len(batch) >= 500:
            _append(out, batch)
            count, batch = count + len(batch), []
    _append(out, batch)
    return count + len(batch)


async def _main(argv: list[str]) -> None:
    settings = Settings.from_env()
    client: GmailClient = build_client(settings)
    connector = GmailSyncConnector(client, await client.get_profile_email())
    if "--perm-sync" in argv:
        print(json.dumps({"permissions": await run_perm_sync(connector, settings.sync_dir)}))
        return
    stats = await run_incremental(connector, settings.sync_dir, full="--full" in argv)
    print(json.dumps(stats.__dict__, ensure_ascii=False))


def main() -> None:
    asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
