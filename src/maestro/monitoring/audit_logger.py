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
        previous_hash = self._latest_event_hash()
        event = {
            "run_id": run_id,
            "timestamp": utc_now().isoformat(),
            "event_type": event_type,
            "details": details,
            "previous_hash": previous_hash,
        }
        event["event_hash"] = _event_hash(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str) + "\n")

    def _latest_event_hash(self) -> str | None:
        if not self.path.exists():
            return None
        last_line = ""
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last_line = line
        if not last_line:
            return None
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError:
            return None
        event_hash = payload.get("event_hash")
        return str(event_hash) if event_hash else None


def _event_hash(event: dict[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()
