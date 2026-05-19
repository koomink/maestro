from maestro.config.models import ExecutionConfig
from maestro.execution.paper import PaperExecutionEngine


def build_execution_engine(
    config: ExecutionConfig,
    *,
    instruments=None,
    currency_sleeves=None,
) -> PaperExecutionEngine:
    if config.proposal_engine == "paper":
        return PaperExecutionEngine(
            config=config,
            instruments=instruments,
            currency_sleeves=currency_sleeves,
        )
    raise ValueError(
        "Unsupported execution proposal engine: "
        f"{config.proposal_engine}. Maestro v0.1.1 supports only 'paper'."
    )
