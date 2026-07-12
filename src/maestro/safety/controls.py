from pydantic import BaseModel

from maestro.core.clock import utc_now
from maestro.core.enums import SafetyState
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore

SAFETY_STATE_EVENT = "safety_state"
SAFETY_EXECUTION_BLOCKED_EVENT = "safety_execution_blocked"
SAFETY_GATE_WARNING_EVENT = "safety_gate_warning"


class SafetySnapshot(BaseModel):
    state: SafetyState
    reason: str
    source: str
    created_at: str
    updated_at: str

    @property
    def blocks_live_execution(self) -> bool:
        return self.state in {SafetyState.PAUSED, SafetyState.KILLED, SafetyState.HALTED}


class SafetyControlService:
    def __init__(self, state_store: StateStore, audit_logger: AuditLogger) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger

    def current_state(self) -> SafetySnapshot:
        latest = self.state_store.load_latest_system_event(SAFETY_STATE_EVENT)
        if latest is None:
            now = utc_now().isoformat()
            return SafetySnapshot(
                state=SafetyState.ACTIVE,
                reason="default",
                source="system",
                created_at=now,
                updated_at=now,
            )
        return SafetySnapshot.model_validate(latest["payload"])

    def pause(self, run_id: str, reason: str, source: str = "cli") -> SafetySnapshot:
        current = self.current_state()
        if current.state == SafetyState.KILLED:
            raise ValueError("kill switch is active and cannot be reset by pause")
        return self._transition(run_id, SafetyState.PAUSED, reason, source, current)

    def resume(self, run_id: str, reason: str, source: str = "cli") -> SafetySnapshot:
        current = self.current_state()
        if current.state == SafetyState.KILLED:
            raise ValueError("kill switch is active and cannot be reset by resume")
        if current.state == SafetyState.HALTED:
            raise ValueError("halted state requires an explicit recovery method")
        return self._transition(run_id, SafetyState.ACTIVE, reason, source, current)

    def clear_halt(self, run_id: str, reason: str, source: str = "cli") -> SafetySnapshot:
        current = self.current_state()
        if current.state == SafetyState.KILLED:
            raise ValueError("kill switch is active and cannot be reset by clear-halt")
        if current.state != SafetyState.HALTED:
            raise ValueError("clear-halt requires current state to be halted")
        return self._transition(run_id, SafetyState.ACTIVE, reason, source, current)

    def release_kill(
        self,
        run_id: str,
        reason: str,
        source: str = "cli",
    ) -> SafetySnapshot:
        current = self.current_state()
        if current.state != SafetyState.KILLED:
            raise ValueError("release-kill requires current state to be killed")
        payload = {
            "reason": reason,
            "source": source,
            "previous_reason": current.reason,
            "killed_at": current.created_at,
        }
        snapshot = self._transition(run_id, SafetyState.ACTIVE, reason, source, current)
        self.state_store.save_system_event(run_id, "safety_kill_released", payload)
        self.audit_logger.log(run_id, "safety_kill_released", payload)
        return snapshot

    def kill_switch(self, run_id: str, reason: str, source: str = "cli") -> SafetySnapshot:
        return self._transition(
            run_id,
            SafetyState.KILLED,
            reason,
            source,
            self.current_state(),
        )

    def halt(self, run_id: str, reason: str, source: str = "system") -> SafetySnapshot:
        current = self.current_state()
        if current.state == SafetyState.KILLED:
            return current
        return self._transition(run_id, SafetyState.HALTED, reason, source, current)

    def record_blocked_execution(
        self,
        run_id: str,
        mode: str,
        safety_state: SafetySnapshot,
        phase: str,
    ) -> None:
        payload = {
            "state": safety_state.state.value,
            "reason": safety_state.reason,
            "source": safety_state.source,
            "mode": mode,
            "phase": phase,
        }
        self.state_store.save_system_event(run_id, SAFETY_EXECUTION_BLOCKED_EVENT, payload)
        self.audit_logger.log(run_id, SAFETY_EXECUTION_BLOCKED_EVENT, payload)

    def record_warning(
        self,
        run_id: str,
        mode: str,
        safety_state: SafetySnapshot,
        phase: str,
    ) -> None:
        payload = {
            "state": safety_state.state.value,
            "reason": safety_state.reason,
            "source": safety_state.source,
            "mode": mode,
            "phase": phase,
        }
        self.state_store.save_system_event(run_id, SAFETY_GATE_WARNING_EVENT, payload)
        self.audit_logger.log(run_id, SAFETY_GATE_WARNING_EVENT, payload)

    def _transition(
        self,
        run_id: str,
        state: SafetyState,
        reason: str,
        source: str,
        current: SafetySnapshot,
    ) -> SafetySnapshot:
        if not reason.strip():
            raise ValueError("safety transition reason is required")
        now = utc_now().isoformat()
        created_at = current.created_at if current.state == state else now
        snapshot = SafetySnapshot(
            state=state,
            reason=reason,
            source=source,
            created_at=created_at,
            updated_at=now,
        )
        payload = snapshot.model_dump(mode="json")
        self.state_store.save_system_event(run_id, SAFETY_STATE_EVENT, payload)
        self.audit_logger.log(run_id, SAFETY_STATE_EVENT, payload)
        return snapshot
