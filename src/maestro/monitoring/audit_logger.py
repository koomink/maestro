import fcntl
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from maestro.core.clock import utc_now


class AuditLogger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, run_id: str, event_type: str, details: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            previous_hash = self._latest_event_hash()
            event = {
                "run_id": run_id,
                "timestamp": utc_now().isoformat(),
                "event_type": event_type,
                "details": details,
                "previous_hash": previous_hash,
            }
            event["event_hash"] = _event_hash(event)
            handle.write(json.dumps(event, default=str) + "\n")

    def _latest_event_hash(self) -> str | None:
        if not self.path.exists():
            return None
        if self.path.stat().st_size == 0:
            return None
        last_line = self._latest_non_empty_line()
        if last_line is None:
            return None
        try:
            payload = json.loads(last_line.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        event_hash = payload.get("event_hash")
        return str(event_hash) if event_hash else None

    def _latest_non_empty_line(self) -> bytes | None:
        chunk_size = 8192
        buffer = b""
        with self.path.open("rb") as handle:
            position = handle.seek(0, 2)
            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
                stripped = buffer.rstrip(b"\r\n")
                lines = stripped.splitlines()
                for index in range(len(lines) - 1, -1, -1):
                    line = lines[index]
                    if not line.strip():
                        continue
                    if index == 0 and position > 0:
                        break
                    return line
                if position == 0:
                    for line in lines:
                        if line.strip():
                            return line
        return None


def _event_hash(event: dict[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()
