"""AuditLogger 구현: 판정 1건 = JSONL 1줄 (logs/audit.jsonl)."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from contracts.permission import AuditRecord

MAX_STR = 500  # 인자 값이 길면(예: write_file content) 감사 로그에는 앞부분만 남긴다


def _clip(v: Any) -> Any:
    if isinstance(v, str) and len(v) > MAX_STR:
        return v[:MAX_STR] + f"…(+{len(v) - MAX_STR} chars)"
    if isinstance(v, dict):
        return {k: _clip(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clip(x) for x in v]
    return v


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


class JsonlAuditLogger:
    def __init__(self, path: str | Path = "logs/audit.jsonl") -> None:
        self.path = Path(path)

    def log(self, record: AuditRecord) -> None:
        d = asdict(record)
        d["call"]["arguments"] = _clip(d["call"]["arguments"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, default=_default) + "\n")
