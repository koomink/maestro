"""Which systemd units have to be down before the state database may be touched.

Stopping the services is not enough on its own. A timer, a ``.path`` unit or a
restart helper left enabled brings a writer back between the check and the
operation, and ``systemctl is-active`` answers "inactive" for a unit whose start
job is merely queued -- it will be running a moment later, inside the migration.

And being down *now* is not enough either. A service that is merely stopped
comes straight back after a host reboot -- multi-user.target pulls in every
enabled unit, and ``Persistent=true`` timers replay anything missed while the
box was down. A migration can outlive an unplanned reboot; it must survive one.

So the barrier is four conditions at once: every writer inactive, every
activator inactive, no queued job for either, and no barrier unit left in an
enablement state that lets systemd start it on its own at boot. On top of all
four the migration also holds the StateStore writer lock for its whole run,
because none of this constrains a cooperating ``maestro`` CLI invocation an
operator runs by hand.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: Units whose process writes the Maestro state database.
WRITER_UNITS: tuple[str, ...] = (
    "maestro-telegram-operator.service",
    # Named "read-only Dashboard" and is not. POST /api/dashboard/refresh
    # refreshes broker and FX state, and POST
    # /api/dashboard/virtuoso/{strategy_id}/generate-signal runs a signal.
    # Both write system_events.
    "maestro-dashboard.service",
    "maestro-heartbeat.service",
    "maestro-fx-refresh.service",
    "maestro-resume-order-tracking.service",
    # Type=oneshot: it finishes on its own, but while it runs it is a writer,
    # and its ExecStopPost starts the telegram operator (see
    # QUIESCE_STOP_ORDER).
    "maestro-run-once.service",
    "maestro-symphony-readonly.service",
    "maestro-symphony-readonly-kr.service",
    "maestro-symphony-readonly-us.service",
    "maestro-symphony-signal.service",
    "maestro-symphony-signal-kr.service",
    "maestro-symphony-signal-us.service",
    "maestro-symphony-rebalance-kr.service",
    "maestro-symphony-rebalance-us.service",
)

#: Units that start or restart a writer without being one themselves. Stopping
#: the writers while any of these is live only wins the race by luck.
ACTIVATOR_UNITS: tuple[str, ...] = (
    "maestro-book-performance.timer",
    "maestro-dashboard-health.timer",
    # A liveness probe that runs `systemctl restart maestro-dashboard.service`.
    "maestro-dashboard-health.service",
    # Triggered by maestro-dashboard.path; restarts the dashboard.
    "maestro-dashboard-reload.service",
    # Watches src/maestro recursively and restarts the dashboard on change --
    # which a deploy performing the upgrade is guaranteed to trigger.
    "maestro-dashboard-src-watch.service",
    "maestro-dashboard.path",
    "maestro-fx-refresh.timer",
    "maestro-heartbeat.timer",
    "maestro-resume-order-tracking.timer",
    "maestro-run-once.timer",
    "maestro-symphony-readonly.timer",
    "maestro-symphony-readonly-kr.timer",
    "maestro-symphony-readonly-us.timer",
    "maestro-symphony-signal.timer",
    "maestro-symphony-signal-kr.timer",
    "maestro-symphony-signal-us.timer",
)

#: Reviewed and cleared: writes no Maestro state. Listed rather than omitted so
#: the inventory test can insist every shipped unit was actually looked at.
NON_WRITER_UNITS: tuple[str, ...] = (
    # Runs a backtest script in the virtuoso checkout and writes a JSON file.
    "maestro-book-performance.service",
)

BARRIER_UNITS: tuple[str, ...] = (*WRITER_UNITS, *ACTIVATOR_UNITS)

#: ``systemctl is-enabled`` answers that provably cannot pull a unit in at
#: boot by themselves. ``static``/``indirect``/``linked`` units have no install
#: target of their own -- they are activated only by their trigger (timer,
#: path, socket), which is itself a barrier member checked above. Anything
#: else -- ``enabled``, ``enabled-runtime``, or an unrecognized answer --
#: counts as capable of auto-starting on reboot: fail closed on states this
#: inventory has not reasoned about.
BOOT_AUTOSTART_SAFE_STATES: frozenset[str] = frozenset(
    {"disabled", "masked", "static", "indirect", "linked"}
)

#: The order the runbook stops units in.
#:
#: Activators first, so nothing restarts what is about to be stopped. Then the
#: one-shot writers, then the long-running ones. ``maestro-run-once.service``
#: comes *before* ``maestro-telegram-operator.service`` on purpose: run-once
#: declares ``ExecStartPre=systemctl stop maestro-telegram-operator.service``
#: and ``ExecStopPost=systemctl start maestro-telegram-operator.service``, so
#: stopping run-once starts the operator. Stopping the operator first would
#: have it brought straight back up by the next command in the sequence.
QUIESCE_STOP_ORDER: tuple[str, ...] = (
    *(unit for unit in ACTIVATOR_UNITS if unit.endswith((".timer", ".path"))),
    "maestro-dashboard-src-watch.service",
    "maestro-dashboard-health.service",
    "maestro-dashboard-reload.service",
    "maestro-symphony-signal.service",
    "maestro-symphony-signal-kr.service",
    "maestro-symphony-signal-us.service",
    "maestro-symphony-rebalance-kr.service",
    "maestro-symphony-rebalance-us.service",
    "maestro-symphony-readonly.service",
    "maestro-symphony-readonly-kr.service",
    "maestro-symphony-readonly-us.service",
    "maestro-fx-refresh.service",
    "maestro-heartbeat.service",
    "maestro-resume-order-tracking.service",
    "maestro-run-once.service",
    "maestro-telegram-operator.service",
    "maestro-dashboard.service",
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class UnitState:
    unit: str
    active: bool
    enabled: str


@dataclass(frozen=True)
class QuiesceReport:
    active_units: tuple[str, ...]
    queued_jobs: tuple[str, ...]
    autostart_units: tuple[str, ...] = ()

    @property
    def quiesced(self) -> bool:
        return not self.active_units and not self.queued_jobs and not self.autostart_units


def _run(runner: Runner, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return runner(  # noqa: S603 - fixed argv, no shell, no user input
        list(args), check=False, capture_output=True, text=True
    )


def _is_active(runner: Runner, unit: str) -> bool:
    return _run(runner, ["systemctl", "is-active", unit]).returncode == 0


def _enabled_state(runner: Runner, unit: str) -> str:
    return (_run(runner, ["systemctl", "is-enabled", unit]).stdout or "").strip()


def _autostart_capable(runner: Runner, unit: str) -> bool:
    """Whether systemd could start this unit on its own after a reboot.

    An unrecognized or empty answer counts as capable: an enablement state the
    inventory has not reasoned about must stop the migration, not pass it.
    """
    return _enabled_state(runner, unit) not in BOOT_AUTOSTART_SAFE_STATES


def _queued_jobs(runner: Runner) -> tuple[str, ...]:
    """Barrier units with a systemd job still queued.

    A queued start job is invisible to ``is-active`` and is precisely the
    failure this barrier exists to prevent: the check passes, the migration
    begins, and systemd starts the writer a second later.
    """
    result = _run(runner, ["systemctl", "list-jobs", "--no-legend"])
    jobs = {
        field
        for line in (result.stdout or "").splitlines()
        for field in line.split()
        if field in BARRIER_UNITS
    }
    return tuple(sorted(jobs))


def capture_unit_states(
    units: Sequence[str] = BARRIER_UNITS, *, run: Runner = subprocess.run
) -> list[UnitState]:
    """The states to restore afterwards -- exactly, not ``enable --now`` on everything.

    Some units are intentionally disabled or masked in a given deployment.
    Turning them all on after a migration would start writers the operator had
    deliberately turned off, which is a worse outcome than the migration itself
    failing.
    """
    states = []
    for unit in units:
        enabled = _run(run, ["systemctl", "is-enabled", unit]).stdout.strip() or "unknown"
        states.append(UnitState(unit=unit, active=_is_active(run, unit), enabled=enabled))
    return states


def verify_quiesced(*, run: Runner = subprocess.run) -> QuiesceReport:
    """Re-check the barrier rather than trusting the stop commands.

    Named, not counted: an operator whose barrier failed needs to know which
    unit is still up, and the answer is frequently a timer nobody thought of --
    or a service that was only *stopped* and would come straight back at the
    next reboot, straight into a MIGRATING database.
    """
    active = tuple(unit for unit in BARRIER_UNITS if _is_active(run, unit))
    autostart = tuple(unit for unit in BARRIER_UNITS if _autostart_capable(run, unit))
    return QuiesceReport(
        active_units=active, queued_jobs=_queued_jobs(run), autostart_units=autostart
    )


__all__ = [
    "ACTIVATOR_UNITS",
    "BARRIER_UNITS",
    "BOOT_AUTOSTART_SAFE_STATES",
    "NON_WRITER_UNITS",
    "QUIESCE_STOP_ORDER",
    "WRITER_UNITS",
    "QuiesceReport",
    "UnitState",
    "capture_unit_states",
    "verify_quiesced",
]
