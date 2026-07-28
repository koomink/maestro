from maestro.dashboard.snapshot import (
    _asset_summary_metrics,
    _freshness_rollup,
    _money,
    _verdict_reason_rows,
)


def test_asset_summary_metrics_label_native_and_converted_currencies():
    metrics = _asset_summary_metrics(
        krw_assets=1000.0,
        usd_assets=20.0,
        krw_total=30000.0,
        usd_total=22.0,
        fx_snapshot={"status": "fresh"},
    )

    assert [metric["label"] for metric in metrics] == [
        "KRW Assets",
        "USD Assets",
        "Total Assets (KRW)",
        "Total Assets (USD)",
        "FX",
    ]
    assert metrics[0]["value"] == "1,000.00 KRW"
    assert metrics[1]["value"] == "20.00 USD"
    assert metrics[2]["value"] == "30,000.00 KRW"
    assert metrics[3]["value"] == "22.00 USD"
    assert _money(1000.0, "UNKNOWN") == "1,000.00"


def test_verdict_reason_rows_explain_status_sources():
    rows = _verdict_reason_rows(
        operator_summary={
            "attention_items": [
                {
                    "severity": "danger",
                    "code": "health_fail",
                    "message": "Health check failed",
                }
            ]
        },
        freshness=[
            {
                "name": "Broker snapshot",
                "status": "stale",
                "age_seconds": 120,
                "max_age_seconds": 60,
            }
        ],
        health={
            "status": "fail",
            "checks": [
                {
                    "name": "State DB",
                    "status": "failed",
                    "message": "sqlite busy",
                }
            ],
        },
        reconciliation={"passed": False},
        live_order_lifecycle={"recent_issue_count": 2},
        fx_snapshot={"status": "stale", "source": "broker_snapshot"},
    )

    assert rows[0]["tone"] == "danger"
    assert any(
        row["source"] == "health_fail" and row["reason"] == "Health check failed" for row in rows
    )
    assert any(row["source"] == "Broker snapshot" and row["status"] == "stale" for row in rows)
    assert any(row["source"] == "State DB" and row["status"] == "failed" for row in rows)
    assert any(row["source"] == "Reconciliation" and row["tone"] == "danger" for row in rows)
    assert any(row["source"] == "Live orders" and row["status"] == "issues" for row in rows)
    assert any(row["source"] == "FX" and row["status"] == "stale" for row in rows)
    assert all(row["next_check"] for row in rows)


def test_freshness_reason_uses_operator_wording_and_a_specific_next_check():
    """The brief is the first thing read on the dashboard, so it must not echo
    the event key that happens to back a check."""
    rows = _verdict_reason_rows(
        operator_summary={},
        freshness=[
            {
                "name": "scheduled_run",
                "status": "stale",
                "age_seconds": 1_676_249,
                "max_age_seconds": 86_400,
            }
        ],
        health={},
        reconciliation={"passed": True},
        live_order_lifecycle={},
        fx_snapshot={"status": "fresh"},
    )

    (row,) = rows
    assert row["reason"] == "Scheduled run is stale — last written 19d ago, limit 24h"
    assert row["next_check"] == "Check the daily signal timers on the host"


def test_freshness_rollup_treats_missing_scheduled_run_as_warning():
    rows = [
        {"name": "broker_snapshot", "status": "fresh"},
        {"name": "broker_reconciliation", "status": "fresh"},
        {"name": "heartbeat", "status": "fresh"},
        {"name": "scheduled_run", "status": "missing"},
    ]

    assert _freshness_rollup(rows) == "warning"
