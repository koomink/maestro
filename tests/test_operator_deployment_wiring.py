import fcntl
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts/operator/symphony_signal_then_approval.sh"
SYSTEMD_DIR = REPO_ROOT / "deploy/systemd"
APP_SERVICE_NAMES = {
    "maestro-book-performance.service",
    "maestro-dashboard.service",
    "maestro-fx-refresh.service",
    "maestro-heartbeat.service",
    "maestro-resume-order-tracking.service",
    "maestro-run-once.service",
    "maestro-symphony-readonly.service",
    "maestro-symphony-readonly-kr.service",
    "maestro-symphony-readonly-us.service",
    "maestro-symphony-rebalance-kr.service",
    "maestro-symphony-rebalance-us.service",
    "maestro-symphony-signal.service",
    "maestro-symphony-signal-kr.service",
    "maestro-symphony-signal-us.service",
    "maestro-telegram-operator.service",
}


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_wrapper(tmp_path: Path, maestro_body: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_path = tmp_path / "calls.log"
    _write_executable(
        bin_dir / "systemctl",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "systemctl $*" >> {calls_path}
""",
    )
    _write_executable(
        bin_dir / "maestro",
        maestro_body,
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MAESTRO_BIN": str(bin_dir / "maestro"),
        "SYSTEMCTL_BIN": str(bin_dir / "systemctl"),
        "MAESTRO_SIGNAL_CONFIG": str(tmp_path / "symphony_signal.yaml"),
        "MAESTRO_APPROVAL_CONFIG": str(tmp_path / "symphony_approval.yaml"),
        "MAESTRO_SIGNAL_LOCK_PATH": str(tmp_path / "symphony.lock"),
    }
    return subprocess.run(
        [str(WRAPPER)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_signal_wrapper_skips_approval_when_no_action(tmp_path):
    result = _run_wrapper(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$(dirname "$0")/maestro-calls.log"
echo "signal_run_id=sig_noop strategies=2 action_required=false orders_preview=0"
""",
    )

    assert result.returncode == 0
    assert "status=no_action" in result.stdout
    calls = (tmp_path / "bin/maestro-calls.log").read_text()
    assert "run-signal --config" in calls
    assert "approve-signal" not in calls
    assert not (tmp_path / "calls.log").exists()


def test_signal_wrapper_hands_actionable_signal_to_approval(tmp_path):
    result = _run_wrapper(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$(dirname "$0")/maestro-calls.log"
if [ "$1" = "run-signal" ]; then
  echo "signal_run_id=sig_action strategies=2 action_required=true orders_preview=3"
else
  echo "signal_run_id=sig_action run_id=run_approval orders=3 approval_status=approved"
fi
""",
    )

    assert result.returncode == 0
    assert "status=approval_required signal_run_id=sig_action" in result.stdout
    calls = (tmp_path / "bin/maestro-calls.log").read_text()
    assert "run-signal --config" in calls
    assert "approve-signal --config" in calls
    assert "--signal-run-id sig_action" in calls
    systemctl_calls = (tmp_path / "calls.log").read_text().splitlines()
    assert systemctl_calls == [
        "systemctl stop maestro-telegram-operator.service",
        "systemctl start maestro-telegram-operator.service",
    ]


def test_signal_wrapper_fails_closed_when_signal_output_is_unparseable(tmp_path):
    result = _run_wrapper(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$(dirname "$0")/maestro-calls.log"
echo "unexpected output"
""",
    )

    assert result.returncode != 0
    assert "status=fail reason=unparseable_run_signal_output" in result.stdout
    calls = (tmp_path / "bin/maestro-calls.log").read_text()
    assert "approve-signal" not in calls


def test_signal_wrapper_fails_when_another_run_holds_the_lock(tmp_path):
    lock_path = tmp_path / "symphony.lock"
    lock_file = lock_path.open("w")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _run_wrapper(
            tmp_path,
            """#!/usr/bin/env bash
set -euo pipefail
echo "should not run"
""",
        )
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

    assert result.returncode != 0
    assert "status=locked" in result.stdout


def test_symphony_systemd_units_wire_operator_configs_and_daily_cli():
    readonly_service = (SYSTEMD_DIR / "maestro-symphony-readonly.service").read_text()
    readonly_timer = (SYSTEMD_DIR / "maestro-symphony-readonly.timer").read_text()
    readonly_kr_service = (SYSTEMD_DIR / "maestro-symphony-readonly-kr.service").read_text()
    readonly_kr_timer = (SYSTEMD_DIR / "maestro-symphony-readonly-kr.timer").read_text()
    readonly_us_service = (SYSTEMD_DIR / "maestro-symphony-readonly-us.service").read_text()
    readonly_us_timer = (SYSTEMD_DIR / "maestro-symphony-readonly-us.timer").read_text()
    signal_service = (SYSTEMD_DIR / "maestro-symphony-signal.service").read_text()
    signal_timer = (SYSTEMD_DIR / "maestro-symphony-signal.timer").read_text()
    telegram_service = (SYSTEMD_DIR / "maestro-telegram-operator.service").read_text()
    legacy_run_once = (SYSTEMD_DIR / "maestro-run-once.service").read_text()

    assert "EnvironmentFile=/etc/maestro/maestro.env" in readonly_service
    assert "kis-sync --config ${MAESTRO_READONLY_CONFIG}" in readonly_service
    assert "--max-age-seconds 3599" in readonly_service
    assert "OnCalendar=hourly" in readonly_timer
    assert "--account-ids kis_isa,kis_ps" in readonly_kr_service
    assert "Asia/Seoul" in readonly_kr_timer
    assert "--account-ids toss_brokerage" in readonly_us_service
    assert "America/New_York" in readonly_us_timer
    assert "daily-signal-approval" in signal_service
    assert "--readonly-config ${MAESTRO_READONLY_CONFIG}" in signal_service
    assert "--signal-config ${MAESTRO_SIGNAL_CONFIG}" in signal_service
    assert "--approval-config ${MAESTRO_APPROVAL_CONFIG}" in signal_service
    assert "symphony_signal_then_approval.sh" not in signal_service
    assert "TimeoutStartSec=1200" in signal_service
    assert "OnCalendar=Mon..Fri 09:10:00 Asia/Seoul" in signal_timer
    assert "OnCalendar=Mon..Fri 09:40:00 America/New_York" in signal_timer
    assert "telegram-operator --config ${MAESTRO_READONLY_CONFIG}" in telegram_service
    assert "--signal-config ${MAESTRO_SIGNAL_CONFIG}" in telegram_service
    assert "Restart=always" in telegram_service
    assert "maestro run-once" in legacy_run_once
    assert "symphony_signal_then_approval.sh" not in legacy_run_once


def test_application_units_run_as_symphony_from_the_home_layout():
    for name in APP_SERVICE_NAMES:
        content = (SYSTEMD_DIR / name).read_text()
        assert "User=symphony" in content
        assert "Group=symphony" in content
        assert "UMask=0077" in content
        assert "/root/projects/Symphony" not in content
        assert "/root/maestro-operator" not in content


def test_deployment_assets_have_no_active_root_runtime_paths():
    paths = [
        *SYSTEMD_DIR.glob("maestro-*"),
        REPO_ROOT / "deploy/maestro-dashboard.service",
        REPO_ROOT / "deploy/scripts/watch_dashboard_backend.sh",
        REPO_ROOT / "scripts/operator/symphony_signal_then_approval.sh",
    ]
    for path in paths:
        content = path.read_text()
        assert "/root/projects/Symphony" not in content
        assert "/root/maestro-operator" not in content


def test_root_watcher_executes_the_root_owned_installed_script():
    content = (SYSTEMD_DIR / "maestro-dashboard-src-watch.service").read_text()
    assert "ExecStart=/bin/bash /usr/local/libexec/maestro/watch_dashboard_backend.sh" in content
    assert "/home/symphony/maestro/deploy/scripts" not in content
