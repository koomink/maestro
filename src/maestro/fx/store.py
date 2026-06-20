from __future__ import annotations

from typing import Protocol

from maestro.core.clock import utc_now
from maestro.fx.models import FXRateSnapshot
from maestro.state.store import StateStore


class FXRateStore(Protocol):
    def load_latest_snapshot(self) -> FXRateSnapshot | None:
        raise NotImplementedError

    def save_snapshot(self, run_id: str, snapshot: FXRateSnapshot) -> None:
        raise NotImplementedError

    def save_failure(
        self,
        run_id: str,
        *,
        provider: str,
        pairs: list[str],
        error: Exception,
    ) -> None:
        raise NotImplementedError


class SystemEventFXRateStore:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load_latest_snapshot(self) -> FXRateSnapshot | None:
        event = self.store.load_latest_system_event("fx_rate_snapshot")
        if event is None:
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            return FXRateSnapshot.model_validate(payload)
        except ValueError:
            return None

    def save_snapshot(self, run_id: str, snapshot: FXRateSnapshot) -> None:
        self.store.save_system_event(
            run_id,
            "fx_rate_snapshot",
            snapshot.as_event_payload(),
        )

    def save_failure(
        self,
        run_id: str,
        *,
        provider: str,
        pairs: list[str],
        error: Exception,
    ) -> None:
        self.store.save_system_event(
            run_id,
            "fx_rate_snapshot_failed",
            {
                "provider": provider,
                "pairs": list(pairs),
                "error_type": type(error).__name__,
                "error_message": _safe_error_message(error),
                "checked_at": utc_now().isoformat(),
            },
        )


def _safe_error_message(error: Exception) -> str:
    message = str(error)
    if len(message) > 500:
        return message[:497] + "..."
    return message


__all__ = ["FXRateStore", "SystemEventFXRateStore"]
