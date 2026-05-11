from maestro.config.models import ExecutionConfig
from maestro.execution.paper import PaperExecutionEngine


def build_execution_engine(
    config: ExecutionConfig,
    *,
    instruments=None,
    currency_sleeves=None,
) -> PaperExecutionEngine:
    if config.engine == "paper":
        return PaperExecutionEngine(
            instruments=instruments,
            currency_sleeves=currency_sleeves,
        )
    raise ValueError(
        f"Unsupported execution engine: {config.engine}. Maestro v0.1.1 supports only 'paper'."
    )
