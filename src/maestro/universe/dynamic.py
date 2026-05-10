from typing import Protocol

from pydantic import BaseModel, Field

from maestro.config.models import UniversePolicyConfig
from maestro.core.enums import BrokerProduct, Currency, ExchangeCode, MarketRegion
from maestro.core.instruments import TradableInstrument
from maestro.sdk import CandidateInstrumentRequest


class BrokerTradabilityChecker(Protocol):
    def is_tradable(self, instrument: TradableInstrument) -> bool:
        raise NotImplementedError


class DataFreshnessChecker(Protocol):
    def has_fresh_data(self, symbol: str, data_types: list[str]) -> bool:
        raise NotImplementedError


class CandidateEvaluation(BaseModel):
    request: CandidateInstrumentRequest
    status: str
    tradable: bool = False
    instrument: TradableInstrument | None = None
    reasons: list[str] = Field(default_factory=list)


class DynamicUniverseApproval(BaseModel):
    approved_symbols: list[str]
    instruments: list[TradableInstrument]
    persistent: bool = False


class InstrumentResolver:
    def __init__(self, existing_instruments: list[TradableInstrument]) -> None:
        self.existing = {instrument.symbol: instrument for instrument in existing_instruments}

    def resolve(self, request: CandidateInstrumentRequest) -> TradableInstrument | None:
        existing = self.existing.get(request.symbol)
        if existing is not None:
            return existing
        if request.intended_use != "tradable":
            return None
        if request.exchange_code is None:
            return None
        return TradableInstrument(
            symbol=request.symbol,
            asset_type=request.asset_type,
            region=MarketRegion(request.region or MarketRegion.US),
            currency=Currency(request.currency or Currency.USD),
            broker="kis",
            broker_product=BrokerProduct(
                request.broker_product or BrokerProduct.KIS_OVERSEAS_STOCK
            ),
            broker_symbol=request.broker_symbol or request.symbol,
            exchange_code=ExchangeCode(request.exchange_code),
            quantity_step=1.0,
            price_tick=0.01,
            min_order_quantity=1.0,
            min_order_notional=1.0,
        )


class DynamicUniverseService:
    def __init__(
        self,
        policy: UniversePolicyConfig,
        resolver: InstrumentResolver,
        *,
        broker_checker: BrokerTradabilityChecker | None = None,
        data_checker: DataFreshnessChecker | None = None,
    ) -> None:
        self.policy = policy
        self.resolver = resolver
        self.broker_checker = broker_checker
        self.data_checker = data_checker

    def evaluate(
        self,
        requests: list[CandidateInstrumentRequest],
        *,
        operator_approved_symbols: set[str] | None = None,
    ) -> list[CandidateEvaluation]:
        approved_symbols = operator_approved_symbols or set()
        evaluations = []
        new_tradable_count = 0
        for request in requests:
            if request.intended_use == "research":
                evaluations.append(
                    CandidateEvaluation(
                        request=request,
                        status="research_only",
                        reasons=["research_universe_only"],
                    )
                )
                continue
            new_tradable_count += 1
            if new_tradable_count > self.policy.max_new_symbols_per_run:
                evaluations.append(
                    CandidateEvaluation(
                        request=request,
                        status="rejected",
                        reasons=["max_new_symbols_per_run_exceeded"],
                    )
                )
                continue
            evaluations.append(self._evaluate_tradable(request, approved_symbols))
        return evaluations

    def approved_tradable_instruments(
        self,
        requests: list[CandidateInstrumentRequest],
        *,
        operator_approved_symbols: set[str],
    ) -> list[TradableInstrument]:
        return [
            evaluation.instrument
            for evaluation in self.evaluate(
                requests,
                operator_approved_symbols=operator_approved_symbols,
            )
            if evaluation.tradable and evaluation.instrument is not None
        ]

    def approve_candidates(
        self,
        requests: list[CandidateInstrumentRequest],
        *,
        operator_approved_symbols: set[str],
        persistent: bool = False,
    ) -> DynamicUniverseApproval:
        instruments = self.approved_tradable_instruments(
            requests,
            operator_approved_symbols=operator_approved_symbols,
        )
        return DynamicUniverseApproval(
            approved_symbols=[instrument.symbol for instrument in instruments],
            instruments=instruments,
            persistent=persistent,
        )

    def _evaluate_tradable(
        self,
        request: CandidateInstrumentRequest,
        approved_symbols: set[str],
    ) -> CandidateEvaluation:
        instrument = self.resolver.resolve(request)
        if instrument is None:
            return CandidateEvaluation(
                request=request,
                status="unresolved",
                reasons=["instrument_unresolved"],
            )
        reasons = self._policy_rejections(instrument)
        if (
            self.policy.require_operator_approval_for_tradable
            and request.symbol not in approved_symbols
        ):
            reasons.append("operator_approval_required")
        if self.policy.require_broker_tradability_check:
            if self.broker_checker is None:
                reasons.append("broker_tradability_check_unavailable")
            elif not self.broker_checker.is_tradable(instrument):
                reasons.append("broker_untradable")
        if self.policy.require_data_freshness_check:
            if self.data_checker is None:
                reasons.append("data_freshness_check_unavailable")
            elif not self.data_checker.has_fresh_data(instrument.symbol, request.data_types):
                reasons.append("stale_or_missing_data")
        if reasons:
            return CandidateEvaluation(
                request=request,
                status="rejected",
                instrument=instrument,
                reasons=reasons,
            )
        return CandidateEvaluation(
            request=request,
            status="approved_tradable",
            tradable=True,
            instrument=instrument,
        )

    def _policy_rejections(self, instrument: TradableInstrument) -> list[str]:
        reasons = []
        if instrument.symbol in self.policy.denied_symbols:
            reasons.append("denied_symbol")
        if set(instrument.asset_tags) & set(self.policy.denied_asset_tags):
            reasons.append("denied_asset_tag")
        if instrument.asset_type not in self.policy.allowed_asset_types:
            reasons.append("asset_type_not_allowed")
        if instrument.region not in self.policy.allowed_regions:
            reasons.append("region_not_allowed")
        if instrument.currency not in self.policy.allowed_currencies:
            reasons.append("currency_not_allowed")
        if instrument.broker_product not in self.policy.allowed_broker_products:
            reasons.append("broker_product_not_allowed")
        if instrument.exchange_code not in self.policy.allowed_exchange_codes:
            reasons.append("exchange_not_allowed")
        return reasons
