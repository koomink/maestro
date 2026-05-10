from pathlib import Path
from typing import Any

import yaml

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import DataBundle, DataRequest
from maestro.state.store import StateStore


def test_us_etf_yahoo_paper_run_uses_fixture_data_without_network(tmp_path):
    raw = yaml.safe_load(Path("configs/us_etf_yahoo_paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "us_etf_yahoo_paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    orchestrator = MaestroOrchestrator(config)
    orchestrator.datahub = FixtureYahooDataHub()

    summary = orchestrator.run_once()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    assert summary.loaded_strategies == ["sample_static_allocation"]
    assert summary.orders_created == 3
    assert summary.cash == 1000.0
    assert store.status()["counts"]["orders"] == 3
    assert {order["payload"]["symbol"] for order in store.list_orders(limit=10)} == {
        "VOO",
        "QQQ",
        "SGOV",
    }
    assert "CASH_USD" in orchestrator.datahub.requested_symbols


class FixtureYahooDataHub:
    prices = {
        "CASH_USD": 1.0,
        "VOO": 500.0,
        "QQQ": 400.0,
        "SGOV": 100.0,
    }

    def __init__(self) -> None:
        self.requested_symbols: list[str] = []

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        now = utc_now()
        data: dict[str, Any] = {}
        for request in requests:
            self.requested_symbols.append(request.symbol)
            data[request.symbol] = {
                "latest_price": {
                    "symbol": request.symbol,
                    "timestamp": now.isoformat(),
                    "price": self.prices[request.symbol],
                    "source": "fixture_yahoo",
                },
                "bars": [],
                "is_stale": False,
                "warnings": [],
            }
        return DataBundle(requests=requests, data=data, generated_at=now, source="fixture_yahoo")
