"""Append-only audit logging without authentication data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, number: int, kind: str = "lesson") -> None:
        self.path = Path("logs") / f"{kind}-{number}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **details) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
