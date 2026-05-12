import importlib
import sys
from datetime import UTC, datetime

from typer.testing import CliRunner

from maestro.cli import app
from maestro.sdk import BaseStrategyPlugin, DataBundle, DataRequest, StrategyContext


def test_init_virtuoso_app_creates_installable_scaffold(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_SECRET", "shell-secret")
    output = tmp_path / "my_app"

    result = CliRunner().invoke(
        app,
        [
            "init-virtuoso-app",
            "--output",
            str(output),
            "--package-name",
            "my_virtuoso_app",
            "--class-name",
            "MyVirtuosoStrategy",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "created app=" in result.output
    assert 'entrypoint="my_virtuoso_app.strategy:MyVirtuosoStrategy"' in result.output
    generated_files = sorted(
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    )
    assert generated_files == [
        "README.md",
        "pyproject.toml",
        "src/my_virtuoso_app/__init__.py",
        "src/my_virtuoso_app/strategy.py",
        "tests/test_strategy_contract.py",
    ]

    generated_text = "\n".join(
        path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file()
    )
    assert "shell-secret" not in generated_text
    assert 'strategy_id="my_virtuoso_app"' in generated_text
    assert "class MyVirtuosoStrategy(BaseStrategyPlugin)" in generated_text


def test_init_virtuoso_app_refuses_overwrite_without_force(tmp_path):
    output = tmp_path / "my_app"
    output.mkdir()
    existing = output / "README.md"
    existing.write_text("existing", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "init-virtuoso-app",
            "--output",
            str(output),
            "--package-name",
            "my_virtuoso_app",
            "--class-name",
            "MyVirtuosoStrategy",
        ],
    )

    assert result.exit_code == 2
    assert "output already exists" in result.output
    assert existing.read_text(encoding="utf-8") == "existing"


def test_init_virtuoso_app_force_overwrites_scaffold_files(tmp_path):
    output = tmp_path / "my_app"
    output.mkdir()
    readme = output / "README.md"
    readme.write_text("existing", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "init-virtuoso-app",
            "--output",
            str(output),
            "--package-name",
            "my_virtuoso_app",
            "--class-name",
            "MyVirtuosoStrategy",
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "# my_virtuoso_app" in readme.read_text(encoding="utf-8")


def test_generated_virtuoso_app_contract(tmp_path):
    output = tmp_path / "my_app"
    result = CliRunner().invoke(
        app,
        [
            "init-virtuoso-app",
            "--output",
            str(output),
            "--package-name",
            "contract_app",
            "--class-name",
            "ContractStrategy",
            "--strategy-id",
            "contract.strategy",
        ],
    )
    assert result.exit_code == 0, result.output

    sys.path.insert(0, str(output / "src"))
    try:
        module = importlib.import_module("contract_app.strategy")
        strategy = module.ContractStrategy()
    finally:
        sys.path.remove(str(output / "src"))

    context = StrategyContext(
        cycle_id="test",
        timestamp=datetime.now(UTC),
        run_mode="paper",
        strategy_id="contract.strategy",
        config={
            "symbols": ["SPY", "CASH"],
            "allocations": {"SPY": 0.7, "CASH": 0.3},
        },
    )
    requests = strategy.build_data_requests(context)
    result = strategy.run(
        data_bundle=DataBundle(
            requests=requests,
            data={},
            generated_at=datetime.now(UTC),
            source="test",
        ),
        context=context,
    )
    manifest = strategy.manifest()

    assert isinstance(strategy, BaseStrategyPlugin)
    assert manifest.sdk_contract_version == "1.0"
    assert manifest.result_type == "target_allocation"
    assert manifest.can_run_live is False
    assert manifest.allow_direct_external_data_calls is False
    assert [request.symbol for request in requests] == ["SPY", "CASH"]
    assert all(isinstance(request, DataRequest) for request in requests)
    assert result.strategy_id == "contract.strategy"
    assert result.allocations == {"SPY": 0.7, "CASH": 0.3}

    source = (output / "src" / "contract_app" / "strategy.py").read_text(encoding="utf-8")
    assert "from maestro.sdk import" in source
    assert "maestro.core" not in source
    assert "maestro.datahub" not in source
    assert "maestro.execution" not in source
    assert "maestro.orchestration" not in source
    assert "maestro.portfolio" not in source
    assert "maestro.risk" not in source
    assert "maestro.state" not in source
