from typing import TYPE_CHECKING, Any

from maestro.config.broker import BrokerAccountConfig, KISConfig
from maestro.execution.broker_router import BrokerAccountRouter, UnsupportedBrokerOperation
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.brokers.readonly import BrokerReadOnlyService
from maestro.execution.brokers.toss.readonly_client import TossReadOnlyClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
from maestro.state.store import StateStore

if TYPE_CHECKING:
    from maestro.config.models import MaestroConfig


class AttributionAwareReadOnlyService:
    def __init__(
        self,
        inner: Any,
        state_store: StateStore,
        audit_logger: AuditLogger,
        *,
        account_id: str,
        strategy_symbols_by_bucket: dict[str, set[str]],
    ) -> None:
        self.inner = inner
        self.state_store = state_store
        self.account_id = account_id
        self.strategy_symbols_by_bucket = strategy_symbols_by_bucket
        self.attribution = AccountAttributionReconciliationService(state_store, audit_logger)

    def fetch_and_store_snapshot(self, symbols: list[str], run_id: str | None = None):
        snapshot = self.inner.fetch_and_store_snapshot(symbols, run_id=run_id)
        stored = _latest_snapshot_for_account(self.state_store, self.account_id)
        if stored is None:
            raise ValueError(
                f"broker snapshot was not stored for attribution account_id={self.account_id}"
            )
        self.attribution.reconcile_broker_snapshot(
            run_id=str(stored["run_id"]),
            account_id=self.account_id,
            broker_snapshot_id=int(stored["id"]),
            broker_positions={
                position.symbol: position.quantity for position in snapshot.account.positions
            },
            strategy_symbols_by_bucket=self.strategy_symbols_by_bucket,
        )
        return snapshot


def broker_readonly_accounts(
    config: "MaestroConfig",
) -> list[tuple[str | None, KISConfig | BrokerAccountConfig]]:
    if config.kis.enabled:
        return [(None, config.kis)]
    if config.accounts:
        return [
            (account.id, account)
            for account in config.accounts
            if account.enabled and account.broker in {"kis", "toss"}
        ]
    return []


def broker_readonly_account_ids(config: "MaestroConfig") -> list[str]:
    return [
        account_id
        for account_id, _ in broker_readonly_accounts(config)
        if account_id is not None
    ]


def build_broker_readonly_service(
    config: "MaestroConfig",
    state_store: StateStore,
    audit_logger: AuditLogger,
    *,
    account_id: str | None = None,
) -> Any:
    if account_id is None and config.kis.enabled:
        return KISReadOnlyService(
            config.kis,
            state_store,
            audit_logger,
            instruments=config.universe.instruments,
            logical_account_id=None,
        )
    account = BrokerAccountRouter(config).account(account_id)
    if account is None:
        raise ValueError(f"Unknown broker account_id: {account_id}")
    if account.broker == "kis":
        service = KISReadOnlyService(
            account.to_kis_config(),
            state_store,
            audit_logger,
            instruments=config.universe.instruments,
            logical_account_id=account.id,
        )
    elif account.broker == "toss":
        service = BrokerReadOnlyService(
            TossReadOnlyClient(account),
            state_store,
            audit_logger,
            logical_account_id=account.id,
            audit_event_type="toss_readonly_snapshot",
        )
    else:
        raise UnsupportedBrokerOperation(
            f"{account.broker} accounts do not support read-only broker operations"
        )
    strategy_symbols = _strategy_symbols_by_bucket(config, account.id)
    if not strategy_symbols:
        return service
    return AttributionAwareReadOnlyService(
        service,
        state_store,
        audit_logger,
        account_id=account.id,
        strategy_symbols_by_bucket=strategy_symbols,
    )


def build_broker_readonly_services(
    config: "MaestroConfig",
    state_store: StateStore,
    audit_logger: AuditLogger,
) -> list[tuple[str | None, Any]]:
    return [
        (
            account_id,
            build_broker_readonly_service(
                config,
                state_store,
                audit_logger,
                account_id=account_id,
            ),
        )
        for account_id, _ in broker_readonly_accounts(config)
    ]


def _strategy_symbols_by_bucket(
    config: "MaestroConfig",
    account_id: str,
) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}
    for bucket_id, target in config.account_strategy_targets.get(account_id, {}).items():
        if bucket_id == "manual":
            continue
        if target.allowed_symbols:
            output[bucket_id] = set(target.allowed_symbols)
            continue
        sleeve = config.execution_sleeves.sleeve(account_id, bucket_id)
        if sleeve is None or sleeve.currency_sleeve is None:
            continue
        currency_sleeve = config.portfolio.currency_sleeves.get(sleeve.currency_sleeve)
        if currency_sleeve is not None:
            output[bucket_id] = set(currency_sleeve.symbols)
    return output


def _latest_snapshot_for_account(
    state_store: StateStore,
    account_id: str,
) -> dict[str, Any] | None:
    for row in state_store.list_broker_account_snapshots(limit=1000):
        payload = row.get("payload") or {}
        logical_account_id = str(payload.get("account_id") or row.get("account_id") or "")
        if logical_account_id == account_id:
            return row
    return None


__all__ = [
    "AttributionAwareReadOnlyService",
    "broker_readonly_account_ids",
    "broker_readonly_accounts",
    "build_broker_readonly_service",
    "build_broker_readonly_services",
]
