"""The barrier is only as complete as its list, so the list is checked."""

from __future__ import annotations

import subprocess
from pathlib import Path

from maestro.ops import quiesce

UNIT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def _shipped() -> set[str]:
    return {path.name for path in UNIT_DIR.iterdir() if path.is_file()}


def _fake_systemctl(
    *,
    active: set[str],
    jobs: list[str],
    enabled: dict[str, str] | None = None,
):
    """``enabled`` maps unit -> is-enabled answer; anything unmapped reports
    "disabled", the safe default, so tests exercise the dimension they name."""

    def run(args, **_kwargs):
        if args[1] == "is-active":
            return subprocess.CompletedProcess(args, 0 if args[2] in active else 3, "", "")
        if args[1] == "is-enabled":
            state = (enabled or {}).get(args[2], "disabled")
            code = 0 if state not in {"", "not-found"} else 1
            return subprocess.CompletedProcess(args, code, f"{state}\n", "")
        if args[1] == "list-jobs":
            body = "".join(f"1 {unit} start running\n" for unit in jobs)
            return subprocess.CompletedProcess(args, 0, body, "")
        raise AssertionError(f"unexpected systemctl call: {args}")

    return run


def test_every_shipped_unit_is_classified():
    """A newly added unit must fail this test rather than escape the barrier."""
    classified = (
        set(quiesce.WRITER_UNITS) | set(quiesce.ACTIVATOR_UNITS) | set(quiesce.NON_WRITER_UNITS)
    )
    assert _shipped() - classified == set()


def test_nothing_is_classified_in_two_places():
    names = [*quiesce.WRITER_UNITS, *quiesce.ACTIVATOR_UNITS, *quiesce.NON_WRITER_UNITS]
    assert len(names) == len(set(names))


def test_no_phantom_units_are_listed():
    """A renamed unit left behind here reads as coverage the barrier no longer has."""
    classified = (
        set(quiesce.WRITER_UNITS) | set(quiesce.ACTIVATOR_UNITS) | set(quiesce.NON_WRITER_UNITS)
    )
    assert classified - _shipped() == set()


def test_the_dashboard_is_treated_as_a_writer():
    """It is called read-only and is not: POST /api/dashboard/refresh and
    POST /api/dashboard/virtuoso/{id}/generate-signal both write state."""
    assert "maestro-dashboard.service" in quiesce.WRITER_UNITS


def test_every_timer_and_path_unit_is_an_activator():
    triggers = {name for name in _shipped() if name.endswith((".timer", ".path"))}
    assert triggers <= set(quiesce.ACTIVATOR_UNITS)


def test_the_dashboard_restart_helpers_are_activators():
    for unit in (
        "maestro-dashboard-health.service",
        "maestro-dashboard-reload.service",
        "maestro-dashboard-src-watch.service",
    ):
        assert unit in quiesce.ACTIVATOR_UNITS


def test_run_once_is_stopped_before_the_operator_it_restarts():
    """run-once's ExecStopPost starts the telegram operator, so stopping the
    operator first would have it brought straight back up."""
    order = list(quiesce.QUIESCE_STOP_ORDER)
    assert order.index("maestro-run-once.service") < order.index(
        "maestro-telegram-operator.service"
    )


def test_activators_are_stopped_before_the_writers_they_start():
    order = list(quiesce.QUIESCE_STOP_ORDER)
    last_trigger = max(
        order.index(unit) for unit in order if unit.endswith((".timer", ".path"))
    )
    assert last_trigger < min(order.index(unit) for unit in quiesce.WRITER_UNITS)


def test_the_stop_order_covers_every_writer_and_activator():
    assert set(quiesce.QUIESCE_STOP_ORDER) == set(quiesce.BARRIER_UNITS)


def test_a_fully_stopped_system_reports_quiesced():
    report = quiesce.verify_quiesced(run=_fake_systemctl(active=set(), jobs=[]))
    assert report.quiesced is True
    assert report.active_units == ()
    assert report.queued_jobs == ()


def test_one_live_writer_is_named_not_counted():
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(active={"maestro-dashboard.service"}, jobs=[])
    )
    assert report.quiesced is False
    assert report.active_units == ("maestro-dashboard.service",)


def test_a_live_timer_breaks_the_barrier_even_with_every_service_down():
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(active={"maestro-symphony-signal-kr.timer"}, jobs=[])
    )
    assert report.quiesced is False
    assert report.active_units == ("maestro-symphony-signal-kr.timer",)


def test_every_live_unit_is_reported_not_just_the_first():
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(active=set(quiesce.BARRIER_UNITS), jobs=[])
    )
    assert set(report.active_units) == set(quiesce.BARRIER_UNITS)


def test_a_queued_start_job_breaks_the_barrier_with_nothing_active():
    """is-active says "inactive" for a unit whose start job is still queued.
    It will be running a moment later -- inside the migration."""
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(active=set(), jobs=["maestro-heartbeat.service"])
    )
    assert report.quiesced is False
    assert report.queued_jobs == ("maestro-heartbeat.service",)


def test_a_queued_job_for_an_unrelated_unit_is_ignored():
    report = quiesce.verify_quiesced(run=_fake_systemctl(active=set(), jobs=["sshd.service"]))
    assert report.quiesced is True


def test_a_stopped_but_enabled_writer_comes_back_at_the_next_reboot():
    """is-active says "inactive" for a unit multi-user.target will start at
    boot -- and Persistent timers replay anything missed while the box was
    down. A migration can outlive an unplanned reboot; inactive-now is not
    reboot-safe."""
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(
            active=set(),
            jobs=[],
            enabled={"maestro-telegram-operator.service": "enabled"},
        )
    )
    assert report.quiesced is False
    assert report.autostart_units == ("maestro-telegram-operator.service",)


def test_every_enabled_unit_is_reported_not_just_one():
    enabled = {unit: "enabled" for unit in quiesce.BARRIER_UNITS}
    report = quiesce.verify_quiesced(run=_fake_systemctl(active=set(), jobs=[], enabled=enabled))
    assert set(report.autostart_units) == set(quiesce.BARRIER_UNITS)


def test_a_masked_or_disabled_unit_cannot_start_itself():
    for state in ("disabled", "masked"):
        report = quiesce.verify_quiesced(
            run=_fake_systemctl(
                active=set(),
                jobs=[],
                enabled={"maestro-dashboard.service": state},
            )
        )
        assert report.quiesced is True, state


def test_a_static_service_cannot_self_start_its_trigger_units_are_checked():
    """run-once has no [Install]: only its timer can pull it in, and the timer
    is a barrier member of its own. Static is not an auto-start risk."""
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(
            active=set(),
            jobs=[],
            enabled={"maestro-run-once.service": "static"},
        )
    )
    assert report.autostart_units == ()


def test_an_unrecognized_enablement_answer_fails_closed():
    """An enablement state this inventory has not reasoned about stops the
    migration rather than passing it."""
    for state in ("", "enabled-runtime", "transient", "generated"):
        report = quiesce.verify_quiesced(
            run=_fake_systemctl(
                active=set(),
                jobs=[],
                enabled={"maestro-heartbeat.timer": state},
            )
        )
        assert report.quiesced is False, repr(state)
        assert report.autostart_units == ("maestro-heartbeat.timer",)


def test_capture_records_what_to_restore():
    states = quiesce.capture_unit_states(
        units=("maestro-heartbeat.timer",),
        run=_fake_systemctl(
            active={"maestro-heartbeat.timer"},
            jobs=[],
            enabled={"maestro-heartbeat.timer": "enabled"},
        ),
    )
    assert states == [
        quiesce.UnitState(unit="maestro-heartbeat.timer", active=True, enabled="enabled")
    ]
