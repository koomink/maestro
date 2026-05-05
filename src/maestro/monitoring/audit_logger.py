import json
from pathlib import Path
from typing import Any

from maestro.core.clock import utc_now


class AuditLogger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, run_id: str, event_type: str, details: dict[str, Any]) -> None:
        event = {
            "run_id": run_id,
            "timestamp": utc_now().isoformat(),
            "event_type": event_type,
            "details": details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str) + "\n")
