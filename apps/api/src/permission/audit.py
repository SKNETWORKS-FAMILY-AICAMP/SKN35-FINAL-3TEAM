"""감사 로그: AuditRecord 1건 = JSONL 1줄 (SYS-17).

나중에 DB(audit_log 테이블)로 옮길 때 같은 필드 이름을 쓰도록 to_dict() 를 맞춰 뒀다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from contracts.permission import AuditRecord

MAX_STR = 500  # 인자 값이 길면(예: 메일 본문) 감사 로그에는 앞부분만 남긴다


def clip(v: Any) -> Any:
    if isinstance(v, str) and len(v) > MAX_STR:
        return v[:MAX_STR] + f"…(+{len(v) - MAX_STR} chars)"
    if isinstance(v, dict):
        return {k: clip(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clip(x) for x in v]
    return v


def to_dict(record: AuditRecord) -> dict[str, Any]:
    call = record.call
    p = call.principal
    return {
        "ts": record.timestamp.isoformat(),
        "call_id": call.call_id,
        "session_id": call.session_id,
        "user_id": p.user_id,
        "agent_id": call.agent_id,
        "run_id": call.run_id,
        "tool_name": call.tool_name,
        "arguments": clip(call.arguments),
        "principal_snapshot": {"user_id": p.user_id, "roles": list(p.roles), **p.attributes},
        "decision": record.result.decision.value,
        "reason": record.result.reason,
        "rule_id": record.result.rule_id,
        "approval": record.approval,
        "executed": record.executed,
        "tool_ok": record.tool_ok,
    }


class JsonlAuditLogger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()  # 동시에 여러 요청이 써도 줄이 섞이지 않게

    async def log(self, record: AuditRecord) -> None:
        line = json.dumps(to_dict(record), ensure_ascii=False, default=str) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)
