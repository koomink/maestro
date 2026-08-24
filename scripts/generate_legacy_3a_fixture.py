"""Generate tests/fixtures/legacy_3a_state.sql from the actual pre-3a-4 binary.

Run this from a git worktree checked out at the legacy baseline commit, so the
rows are written by the code that really wrote them in production rather than
by hand-shaped inserts through the current StateStore:

    git worktree add /tmp/maestro-legacy 209ed4f18ed57773a72ab4a146e49efae1747348
    cd /tmp/maestro-legacy
    <path-to>/.venv/bin/python <path-to>/scripts/generate_legacy_3a_fixture.py \
        --out <path-to>/tests/fixtures
    git worktree remove /tmp/maestro-legacy

The script refuses to run unless ``maestro`` actually resolves inside the
worktree *and* ``maestro.state.funding_workflow`` does not exist there. The
second check is the real one: that module was added by 3a-4, so its presence
means the current generation is loaded and the output would not be legacy at
all. An editable install of the working checkout will otherwise satisfy the
import silently.

It emits two files:

``legacy_3a_state.sql``     a deterministic ``.dump``, rows ordered by id, with
                           the source SHA in a header comment.
``legacy_3a_state.json``    the *old binary's own answers* about which requests
                           are pending, produced by calling its
                           ``_load_pending_funding_request`` /
                           ``_load_pending_budget_request`` directly. This is
                           what pins the migration tests to the legacy
                           interpretation rather than to a re-description of it.

Nothing here has a clock in it. The fixture has to be byte-reproducible from
the pinned commit, or it stops representing anything in particular.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

LEGACY_SHA = "209ed4f18ed57773a72ab4a146e49efae1747348"

SCOPE = {
    "contribution_group_id": None,
    "account_id": "paper_cash",
    "execution_sleeve": "krw_contribution",
    "currency": "KRW",
}


def _refuse_if_current_generation(worktree: Path) -> None:
    import maestro.state.store

    resolved = Path(maestro.state.store.__file__).resolve()
    if worktree.resolve() not in resolved.parents:
        raise SystemExit(
            f"maestro resolved to {resolved}, outside the worktree {worktree}. "
            "An editable install of the working checkout is shadowing it; run "
            "with PYTHONPATH pointing at the worktree's src."
        )
    if (worktree / "src" / "maestro" / "state" / "funding_workflow.py").exists():
        raise SystemExit(
            "funding_workflow.py exists in this checkout, so it is not the "
            f"pre-3a-4 baseline. Check out {LEGACY_SHA}."
        )


def _request(request_id: str, *, month_key: str, status: str = "pending") -> dict:
    return {
        "request_id": request_id,
        "status": status,
        "month_key": month_key,
        "strategy_ids": ["tranquillo"],
        **SCOPE,
    }


def _seed(store) -> None:
    """Every scenario the migration has to tell apart, in one database."""
    # 1. One unambiguous pending funding request.
    store.save_system_event(
        "run_req-clean",
        "contribution_funding_request",
        {
            **_request("req-clean", month_key="2026-08"),
            "duplicate_key": "contribution_funding_request:req-clean",
        },
    )
    # 2. Two pending funding requests in one workflow (same scope, same month).
    #    The old binary offered a working Confirm button on both.
    for request_id in ("req-amb-a", "req-amb-b"):
        store.save_system_event(
            f"run_{request_id}",
            "contribution_funding_request",
            {
                **_request(request_id, month_key="2026-09"),
                "duplicate_key": f"contribution_funding_request:{request_id}",
            },
        )
    # 3. A funding request the operator already acknowledged.
    store.save_system_event(
        "run_req-acked",
        "contribution_funding_request",
        {
            **_request("req-acked", month_key="2026-10"),
            "duplicate_key": "contribution_funding_request:req-acked",
        },
    )
    store.save_system_event(
        "run_req-acked",
        "contribution_funding_request_ack",
        {
            "request_id": "req-acked",
            "status": "acknowledged",
            "decided_by": "operator",
            "duplicate_key": "funding-ack:req-acked",
        },
    )
    # 4. A budget request the operator already decided.
    store.save_system_event(
        "run_req-budget-decided",
        "contribution_budget_request",
        {
            **_request("req-budget-decided", month_key="2026-11"),
            "duplicate_key": "contribution_budget_request:req-budget-decided",
        },
    )
    store.save_system_event(
        "run_req-budget-decided",
        "contribution_budget_request_decision",
        {
            "request_id": "req-budget-decided",
            "status": "selected",
            "selected_budget": 3000000.0,
            "decided_by": "operator",
            **SCOPE,
            "month_key": "2026-11",
            "strategy_ids": ["tranquillo"],
            "duplicate_key": "budget-decision:req-budget-decided",
        },
    )
    # 5. A legacy approval that provably completed: one group on the run, and a
    #    completion for it.
    store.save_system_event(
        "run_ap-done",
        "telegram_approval_pending",
        {"approval_id": "ap-legacy-done", "run_id": "run_ap-done", "signal_run_id": "sig-done"},
    )
    store.save_system_event(
        "run_ap-done",
        "telegram_approval_ack",
        {"approval_id": "ap-legacy-done", "status": "approved"},
    )
    store.save_system_event(
        "sig-done", "signal_approval_completed", {"signal_run_id": "sig-done"}
    )
    # 6. A legacy approval that was persisted -- execution may have been entered
    #    -- with no resolution to say how it ended.
    store.save_system_event(
        "run_ap-entered",
        "telegram_approval_pending",
        {
            "approval_id": "ap-legacy-entered",
            "run_id": "run_ap-entered",
            "signal_run_id": "sig-entered",
        },
    )
    store.save_system_event(
        "run_ap-entered",
        "telegram_approval_ack",
        {"approval_id": "ap-legacy-entered", "status": "approved"},
    )
    store.save_approval("run_ap-entered", "ap-legacy-entered", {"decision": {"status": "approved"}})
    # 7. A legacy approval on a two-group run whose completion names no group.
    for approval_id in ("ap-legacy-unknown", "ap-legacy-sibling"):
        store.save_system_event(
            f"run_{approval_id}",
            "telegram_approval_pending",
            {
                "approval_id": approval_id,
                "run_id": f"run_{approval_id}",
                "signal_run_id": "sig-multi",
            },
        )
    store.save_system_event(
        "run_ap-legacy-unknown",
        "telegram_approval_ack",
        {"approval_id": "ap-legacy-unknown", "status": "approved"},
    )
    store.save_system_event(
        "sig-multi", "signal_approval_completed", {"signal_run_id": "sig-multi"}
    )
    # 8. A dispatch consumed before manifests existed, never settled.
    store.mark_signal_package_consumed("sig-nomanifest", "run_approval-nomanifest")


def _legacy_pending_answers(store) -> dict[str, dict[str, bool]]:
    """What the pre-3a-4 handler itself says about each request.

    The unbound methods are called against a stub carrying only ``store``,
    which is all they touch. That runs the legacy decision procedure rather
    than a re-description of it, which is the entire point of the pin.
    """
    from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter

    class _Stub:
        pass

    stub = _Stub()
    stub.store = store
    funding = {
        request_id: TelegramOperatorCommandRouter._load_pending_funding_request(stub, request_id)
        is not None
        for request_id in ("req-clean", "req-amb-a", "req-amb-b", "req-acked")
    }
    budget = {
        request_id: TelegramOperatorCommandRouter._load_pending_budget_request(stub, request_id)
        is not None
        for request_id in ("req-budget-decided",)
    }
    return {"pending_funding": funding, "pending_budget": budget}


FROZEN_AT = "2026-01-01 00:00:00"


def _freeze_timestamps(conn: sqlite3.Connection) -> None:
    """Pin every ``created_at`` so the dump is byte-reproducible.

    The columns default to CURRENT_TIMESTAMP, so a regenerated fixture would
    otherwise differ from the checked-in one on every run and the
    reproducibility test could never pass. Nothing under test reads these
    values -- the migration orders by ``system_events.id`` throughout -- so
    freezing them removes noise rather than hiding anything.
    """
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "created_at" in columns:
            conn.execute(f"UPDATE {table} SET created_at = ?", (FROZEN_AT,))  # noqa: S608
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    worktree = Path.cwd()
    sys.path.insert(0, str(worktree / "src"))
    importlib.invalidate_caches()
    _refuse_if_current_generation(worktree)
    from maestro.state.store import StateStore

    head = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != LEGACY_SHA:
        raise SystemExit(f"expected to run at {LEGACY_SHA}, found {head}")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "legacy.sqlite"
        store = StateStore(db_path, 1_000_000.0)
        _seed(store)
        answers = _legacy_pending_answers(store)
        with sqlite3.connect(db_path) as conn:
            _freeze_timestamps(conn)
            dump = "\n".join(conn.iterdump())

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "legacy_3a_state.sql").write_text(
        "-- Generated by scripts/generate_legacy_3a_fixture.py\n"
        f"-- Source commit: {LEGACY_SHA} (pre-3a-4: no funding_workflow, no\n"
        "-- signal_dispatch_manifest). Regenerate with the worktree procedure in\n"
        "-- that script's docstring; the output must be byte-identical.\n"
        f"{dump}\n"
    )
    (args.out / "legacy_3a_state.json").write_text(
        json.dumps({"source_commit": LEGACY_SHA, **answers}, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote {args.out}/legacy_3a_state.sql and legacy_3a_state.json")


if __name__ == "__main__":
    main()
