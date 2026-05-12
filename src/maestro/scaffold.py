from __future__ import annotations

import keyword
import re
import textwrap
from pathlib import Path

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STRATEGY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def create_virtuoso_app_scaffold(
    *,
    output: Path,
    package_name: str,
    class_name: str,
    strategy_id: str | None = None,
    force: bool = False,
) -> None:
    package_name = _validate_python_identifier(package_name, "package-name")
    class_name = _validate_python_identifier(class_name, "class-name")
    strategy_id = _validate_strategy_id(strategy_id or package_name)

    if output.exists() and not force:
        raise FileExistsError("output already exists; pass --force to overwrite")
    if output.exists() and not output.is_dir():
        raise NotADirectoryError("output exists and is not a directory")

    package_dir = output / "src" / package_name
    tests_dir = output / "tests"
    package_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)

    files = {
        output / "pyproject.toml": _pyproject(package_name),
        output / "README.md": _readme(package_name, class_name, strategy_id),
        package_dir / "__init__.py": _init_py(class_name),
        package_dir / "strategy.py": _strategy_py(package_name, class_name, strategy_id),
        tests_dir / "test_strategy_contract.py": _contract_test_py(
            package_name,
            class_name,
            strategy_id,
        ),
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")


def _validate_python_identifier(value: str, field_name: str) -> str:
    if not _IDENTIFIER_RE.match(value) or keyword.iskeyword(value):
        raise ValueError(f"{field_name} must be a valid Python identifier")
    return value


def _validate_strategy_id(value: str) -> str:
    if not value or not _STRATEGY_ID_RE.match(value):
        raise ValueError(
            "strategy-id must contain only letters, numbers, dots, dashes, or underscores"
        )
    return value


def _pyproject(package_name: str) -> str:
    distribution_name = package_name.replace("_", "-")
    return textwrap.dedent(
        f"""\
        [build-system]
        requires = ["setuptools>=68"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "{distribution_name}"
        version = "0.1.0"
        description = "Virtuoso strategy app for Maestro."
        readme = "README.md"
        requires-python = ">=3.11"
        dependencies = []

        [tool.setuptools.packages.find]
        where = ["src"]

        [tool.pytest.ini_options]
        pythonpath = ["src"]

        [tool.ruff]
        target-version = "py311"
        line-length = 100

        [tool.ruff.lint]
        select = ["E", "F", "I", "UP", "B"]

        [tool.ruff.format]
        quote-style = "double"
        indent-style = "space"
        """
    )


def _readme(package_name: str, class_name: str, strategy_id: str) -> str:
    return textwrap.dedent(
        f"""\
        # {package_name}

        Virtuoso strategy app scaffold for Maestro.

        This package is intentionally outside Maestro core. Keep strategy-specific
        wrapper and adapter code in this app repository, and import Maestro only
        through `maestro.sdk`.

        ## Install

        Maestro must be available in the environment that runs this app. During
        local development, install both packages in editable mode:

        ```bash
        uv pip install -e /path/to/Maestro
        uv pip install -e .
        ```

        ## Configure Maestro

        ```yaml
        strategies:
          - id: {strategy_id}
            enabled: true
            mode: paper
            weight: 1.0
            entrypoint: "{package_name}.strategy:{class_name}"
            config:
              symbols: ["SPY", "CASH"]
              allocations:
                SPY: 0.6
                CASH: 0.4
        ```

        ## Wrapper Boundary

        - Import only from `maestro.sdk`.
        - Request market and research data with `DataRequest`.
        - Return `TargetAllocationResult` for SDK contract `1.0`.
        - Do not import Maestro DataHub, broker, execution, state, approval,
          orchestration, or other internals.
        - Keep raw upstream strategy integration in the TODO functions in
          `src/{package_name}/strategy.py`.

        ## Verify

        ```bash
        ruff format --check .
        ruff check .
        pytest -q
        ```
        """
    )


def _init_py(class_name: str) -> str:
    return textwrap.dedent(
        f"""\
        from .strategy import {class_name}

        __all__ = ["{class_name}"]
        """
    )


def _strategy_py(package_name: str, class_name: str, strategy_id: str) -> str:
    return textwrap.dedent(
        f'''\
        from typing import Any

        from maestro.sdk import (
            BaseStrategyPlugin,
            DataBundle,
            DataRequest,
            StrategyContext,
            StrategyManifest,
            TargetAllocationResult,
        )


        class {class_name}(BaseStrategyPlugin):
            def manifest(self) -> StrategyManifest:
                return StrategyManifest(
                    sdk_contract_version="1.0",
                    strategy_id="{strategy_id}",
                    name="{package_name}",
                    version="0.1.0",
                    description="Virtuoso strategy wrapper for Maestro.",
                    supported_modes=["paper"],
                    supported_asset_types=["cash", "stock", "etf", "domestic_etf", "us_etf"],
                    result_type="target_allocation",
                    requires_data=["price"],
                    can_run_live=False,
                    allow_direct_external_data_calls=False,
                )

            def build_data_requests(
                self,
                context: StrategyContext,
            ) -> list[DataRequest]:
                return [
                    DataRequest(
                        symbol=symbol,
                        asset_type=self._asset_type(symbol),
                        data_type="price",
                    )
                    for symbol in self._symbols(context)
                ]

            def run(
                self,
                data_bundle: DataBundle,
                context: StrategyContext,
            ) -> TargetAllocationResult:
                raw_inputs = self._to_raw_strategy_input(data_bundle, context)
                raw_signal = self._run_raw_strategy(raw_inputs)
                allocations = self._signal_to_allocations(raw_signal, context)
                return TargetAllocationResult(
                    strategy_id="{strategy_id}",
                    strategy_version=self.manifest().version,
                    timestamp=context.timestamp,
                    allocations=allocations,
                    confidence=float(context.config.get("confidence", 0.5)),
                    time_horizon=context.config.get("time_horizon", "wrapper-scaffold"),
                    rationale="Scaffold allocation policy. Replace TODO wrapper functions.",
                    metadata={{"raw_signal": raw_signal}},
                )

            def _symbols(self, context: StrategyContext) -> list[str]:
                symbols = context.config.get("symbols")
                if symbols:
                    return list(symbols)
                allocations = self._configured_allocations(context)
                return list(allocations) or ["CASH"]

            def _configured_allocations(self, context: StrategyContext) -> dict[str, float]:
                return dict(context.config.get("allocations", {{"CASH": 1.0}}))

            def _asset_type(self, symbol: str) -> str:
                if symbol == "CASH" or symbol.startswith("CASH_"):
                    return "cash"
                return "etf"

            def _to_raw_strategy_input(
                self,
                data_bundle: DataBundle,
                context: StrategyContext,
            ) -> dict[str, Any]:
                # TODO: Convert Maestro SDK data into the raw upstream strategy input shape.
                return {{
                    "symbols": self._symbols(context),
                    "data": data_bundle.data,
                }}

            def _run_raw_strategy(self, raw_inputs: dict[str, Any]) -> dict[str, Any]:
                # TODO: Call or adapt the original strategy package here.
                return {{"source": "scaffold", "symbols": raw_inputs["symbols"]}}

            def _signal_to_allocations(
                self,
                raw_signal: dict[str, Any],
                context: StrategyContext,
            ) -> dict[str, float]:
                # TODO: Convert raw strategy signals into a Maestro target allocation policy.
                del raw_signal
                return self._configured_allocations(context)
        '''
    )


def _contract_test_py(package_name: str, class_name: str, strategy_id: str) -> str:
    return textwrap.dedent(
        f'''\
        from datetime import UTC, datetime
        from pathlib import Path

        from {package_name}.strategy import {class_name}

        from maestro.sdk import (
            BaseStrategyPlugin,
            DataBundle,
            DataRequest,
            StrategyContext,
            TargetAllocationResult,
        )


        def test_strategy_contract_and_sdk_boundary():
            strategy = {class_name}()
            context = StrategyContext(
                cycle_id="test",
                timestamp=datetime.now(UTC),
                run_mode="paper",
                strategy_id="{strategy_id}",
                config={{
                    "symbols": ["SPY", "CASH"],
                    "allocations": {{"SPY": 0.6, "CASH": 0.4}},
                }},
            )

            requests = strategy.build_data_requests(context)
            result = strategy.run(
                data_bundle=DataBundle(
                    requests=requests,
                    data={{}},
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
            assert all(isinstance(request, DataRequest) for request in requests)
            assert [request.data_type for request in requests] == ["price", "price"]
            assert isinstance(result, TargetAllocationResult)
            assert result.strategy_id == "{strategy_id}"
            assert result.allocations == {{"SPY": 0.6, "CASH": 0.4}}
            assert result.metadata["raw_signal"]["source"] == "scaffold"

            source = Path("src/{package_name}/strategy.py").read_text(encoding="utf-8")
            assert "from maestro.sdk import" in source
            forbidden_imports = [
                "maestro.approval",
                "maestro.config",
                "maestro.core",
                "maestro.datahub",
                "maestro.execution",
                "maestro.integrations",
                "maestro.monitoring",
                "maestro.orchestration",
                "maestro.plugins",
                "maestro.portfolio",
                "maestro.risk",
                "maestro.safety",
                "maestro.signals",
                "maestro.state",
            ]
            assert not any(import_path in source for import_path in forbidden_imports)
        '''
    )
