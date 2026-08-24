"""The two operator commands: look at a batch, then close it.

`approval-outcome` is the one you run first, and it writes nothing -- it
exists so the operator can see what they are about to close. `approval-settle`
closes it, and refuses without an explicit confirmation, the same convention
`release-kill` uses.
"""

import json
import sqlite3
from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.state.store import StateStore

APPROVAL_ID = "appr_1589437a40424cd7a4e7141dbdf96e17"


def _config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
    }
    config_path = tmp_path / "operator.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _proposed(order_id, symbol, side, quantity):
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": 1.0,
    }


def _seed(config_path, *, with_result=True):
    """The 08-11 shape: TIP filled, SSO cancelled unfilled, PDBC never sent."""
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, initial_cash=1000)
    store.save_system_event(
        "run-1",
        "telegram_approval_pending",
        {
            "approval_id": APPROVAL_ID,
            "run_id": "run-1",
            "signal_run_id": "signal-1",
            "request": {
                "approval_id": APPROVAL_ID,
                "proposed_orders": [
                    _proposed("ord_pdbc", "PDBC", "buy", 366.0),
                    _proposed("ord_sso", "SSO", "sell", 20.0),
                    _proposed("ord_tip", "TIP", "sell", 23.0),
                ],
            },
        },
    )
    store.save_system_event(
        "run-1",
        "telegram_approval_ack",
        {
            "approval_id": APPROVAL_ID,
            "signal_run_id": "signal-1",
            "status": "approved",
            "schema_version": 2,
        },
    )
    for order_id, symbol, quantity in (("ord_sso", "SSO", 20.0), ("ord_tip", "TIP", 23.0)):
        request = {
            "order_id": order_id,
            "symbol": symbol,
            "side": "sell",
            "quantity": quantity,
            "approval_id": APPROVAL_ID,
        }
        store.save_system_event("run-1", "live_order_submit_intent", {"request": request})
        if with_result:
            store.save_system_event(
                "run-1",
                "live_order_result",
                {
                    "request": request,
                    "result": {
                        "order_id": order_id,
                        "broker_order": {"broker_order_id": f"brk_{order_id}"},
                        "filled_quantity": 0.0,
                    },
                },
            )
    with sqlite3.connect(config.state.sqlite_path) as conn:
        conn.executemany(
            "INSERT INTO fill_watermarks "
            "(broker_order_id, cumulative_quantity, cumulative_notional) VALUES (?, ?, 0.0)",
            [("brk_ord_tip", 23.0), ("brk_ord_sso", 0.0)],
        )
    store.save_system_event(
        "run-1",
        "live_order_tracking_resolved",
        {"order_id": "ord_sso", "final_status": "canceled"},
    )
    return store


def _completed(store):
    return store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    )


# --- approval-outcome ------------------------------------------------------


def test_outcome_reports_every_order_and_its_classification(tmp_path):
    config_path = _config_path(tmp_path)
    _seed(config_path)

    result = CliRunner().invoke(
        app, ["approval-outcome", "--config", str(config_path), "--approval-id", APPROVAL_ID]
    )

    assert result.exit_code == 0
    assert "PDBC" in result.stdout
    assert "not_sent" in result.stdout
    assert "cancelled_unfilled" in result.stdout
    assert "filled" in result.stdout


def test_outcome_writes_nothing(tmp_path):
    config_path = _config_path(tmp_path)
    store = _seed(config_path)

    CliRunner().invoke(
        app, ["approval-outcome", "--config", str(config_path), "--approval-id", APPROVAL_ID]
    )

    assert _completed(store) == []


def test_outcome_flags_a_batch_that_needs_a_broker_check(tmp_path):
    config_path = _config_path(tmp_path)
    _seed(config_path, with_result=False)

    result = CliRunner().invoke(
        app, ["approval-outcome", "--config", str(config_path), "--approval-id", APPROVAL_ID]
    )

    assert result.exit_code == 0
    assert "has_unknown=True" in result.stdout


def test_outcome_on_an_unknown_approval_exits_nonzero(tmp_path):
    config_path = _config_path(tmp_path)
    _seed(config_path)

    result = CliRunner().invoke(
        app, ["approval-outcome", "--config", str(config_path), "--approval-id", "appr_nope"]
    )

    assert result.exit_code == 1


# --- approval-settle -------------------------------------------------------


def _settle(config_path, *extra, reason="handled at next open"):
    return CliRunner().invoke(
        app,
        [
            "approval-settle",
            "--config",
            str(config_path),
            "--approval-id",
            APPROVAL_ID,
            "--reason",
            reason,
            *extra,
        ],
    )


def test_settle_refuses_without_the_confirmation_token(tmp_path):
    config_path = _config_path(tmp_path)
    store = _seed(config_path)

    result = _settle(config_path)

    assert result.exit_code != 0
    assert _completed(store) == []


def test_settle_refuses_a_wrong_confirmation_token(tmp_path):
    config_path = _config_path(tmp_path)
    store = _seed(config_path)

    result = _settle(config_path, "--confirm", "yes")

    assert result.exit_code != 0
    assert _completed(store) == []


def test_settle_closes_the_approval_and_records_the_operators_reason(tmp_path):
    config_path = _config_path(tmp_path)
    store = _seed(config_path)

    result = _settle(config_path, "--confirm", "SETTLE")

    assert result.exit_code == 0
    payload = _completed(store)[0]["payload"]
    assert payload["settled_by"] == "operator"
    assert payload["reason"] == "handled at next open"


def test_settle_refuses_a_batch_with_an_order_that_may_be_live(tmp_path):
    config_path = _config_path(tmp_path)
    store = _seed(config_path, with_result=False)

    result = _settle(config_path, "--confirm", "SETTLE")

    assert result.exit_code == 1
    assert "unknown_orders" in result.stdout
    assert _completed(store) == []


def test_settle_accepts_an_unknown_batch_the_operator_reconciled(tmp_path):
    config_path = _config_path(tmp_path)
    store = _seed(config_path, with_result=False)

    result = _settle(
        config_path, "--confirm", "SETTLE", "--i-have-reconciled-with-broker"
    )

    assert result.exit_code == 0
    assert _completed(store)[0]["payload"]["reconciled_with_broker"] is True


def test_settle_is_refused_the_second_time(tmp_path):
    config_path = _config_path(tmp_path)
    store = _seed(config_path)
    _settle(config_path, "--confirm", "SETTLE")

    result = _settle(config_path, "--confirm", "SETTLE")

    assert result.exit_code == 1
    assert "already_settled" in result.stdout
    assert len(_completed(store)) == 1


def test_settle_makes_rollback_preflight_safe(tmp_path):
    config_path = _config_path(tmp_path)
    _seed(config_path)
    before = CliRunner().invoke(
        app,
        ["approval-rollback-preflight", "--config", str(config_path), "--no-require-quiesce"],
    )
    assert before.exit_code == 1

    _settle(config_path, "--confirm", "SETTLE")

    after = CliRunner().invoke(
        app,
        ["approval-rollback-preflight", "--config", str(config_path), "--no-require-quiesce"],
    )
    assert after.exit_code == 0
    assert "status=safe failures=0" in after.stdout


def test_settle_reports_a_batch_that_is_being_executed_right_now(tmp_path, monkeypatch):
    """Settlement waits on live_order_lock, and a resume can be holding it.

    The operator asked to close a batch the system is in the middle of
    executing. A traceback tells them the command broke; what is true is that
    the batch is moving under them and there is nothing to settle yet.
    """
    config_path = _config_path(tmp_path)
    store = _seed(config_path)

    def _busy(*args, **kwargs):
        raise TimeoutError("Live order lock is busy: /tmp/state.db.live-order.lock")

    monkeypatch.setattr("maestro.cli.settle_approval", _busy)

    result = _settle(config_path, "--confirm", "SETTLE")

    assert result.exit_code == 1
    assert "status=busy" in result.stdout
    assert _completed(store) == []


# --- audit -----------------------------------------------------------------


def test_both_commands_leave_an_audit_trail(tmp_path):
    config_path = _config_path(tmp_path)
    _seed(config_path)

    CliRunner().invoke(
        app, ["approval-outcome", "--config", str(config_path), "--approval-id", APPROVAL_ID]
    )
    _settle(config_path, "--confirm", "SETTLE")

    entries = [
        json.loads(line)
        for line in Path(tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    actions = {entry.get("action") or entry.get("event_type") for entry in entries}
    assert "approval_outcome_inspected" in actions
    assert "telegram_approval_resolution_completed" in actions
