"""Per-session trace log (JSONL).

Every observation, decision, policy verdict, approval, action, and verification
is appended here as one JSON object per line. This is the substrate for replay,
the OSWorld-style eval metrics, and the post-session Atlas memory export. It
never stores raw screenshots — only paths and structured evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson


class TraceLog:
    def __init__(self, session_id: str, session_dir: str | Path) -> None:
        self.session_id = session_id
        self._dir = Path(session_dir) / session_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "trace.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "t": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "kind": kind,
            **fields,
        }
        with self._path.open("ab") as fh:
            fh.write(orjson.dumps(entry))
            fh.write(b"\n")
        return entry

    def read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self._path.read_bytes().splitlines():
            if line.strip():
                out.append(orjson.loads(line))
        return out

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.read() if e.get("kind") == kind]
