from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct, OrderStatus
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.base import KISRestBaseClient
from maestro.execution.brokers.kis.client import KISReadOnlyClient
from maestro.execution.brokers.kis.models import (
    KISAccountSnapshot,
    KISBuyingPower,
    KISCashBalance,
    KISOrderSummary,
    KISPosition,
)
from maestro.execution.brokers.kis.parsers import (
    _as_list,
    _first_float,
    _first_item,
    _optional_first_float,
    _overseas_order_summary,
    _overseas_position,
    _quote_exchange_code,
)


class KISRestOverseasStockReadOnlyClient(KISRestBaseClient, KISReadOnlyClient):
    request_error_context = "KIS overseas read-only request"
    pagination_error_context = "KIS overseas read-only pagination"

    def get_account_snapshot(self) -> KISAccountSnapshot:
        positions, cash_balance = self._fetch_balance()
        buying_power = self.get_buying_power()
        return KISAccountSnapshot(
            account_id=self.credentials.account_id,
            cash=cash_balance.cash,
            cash_by_currency={"USD": cash_balance.cash},
            buying_power=buying_power.cash_buying_power,
            positions=positions,
            cash_balance=cash_balance,
            buying_power_detail=buying_power,
            ledger_cash_by_currency={"USD": cash_balance.cash},
            buying_power_by_currency={"USD": buying_power.cash_buying_power},
            fetched_at=utc_now(),
            source="kis_overseas_stock_readonly",
        )

    def get_positions(self) -> list[KISPosition]:
        positions, _ = self._fetch_balance()
        return positions

    def get_buying_power(
        self,
        symbol: str | None = None,
        order_price: float | None = None,
        currency: str | None = None,
    ) -> KISBuyingPower:
        # One market, one currency: `currency` cannot change the answer, but the
        # reply names USD so a mis-routed order in another currency is caught by
        # the caller instead of being paid for out of the dollar balance.
        broker_symbol, exchange_code = self._buying_power_symbol(symbol)
        price_text = "1" if order_price is None else _format_kis_decimal(order_price)
        payload = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            self._tr_id(real="TTTS3007R", demo="VTTS3007R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "OVRS_EXCG_CD": exchange_code,
                "OVRS_ORD_UNPR": price_text,
                "ITEM_CD": broker_symbol,
            },
        )
        output = _first_item(payload.get("output"))
        return KISBuyingPower(
            symbol=symbol or self._canonical_symbol(broker_symbol),
            order_price=_optional_first_float(output, "ovrs_ord_unpr", "ord_unpr") or order_price,
            cash_buying_power=_first_float(
                output,
                "ovrs_ord_psbl_amt",
                "frcr_ord_psbl_amt1",
                "ord_psbl_amt",
                "max_ord_psbl_amt",
            ),
            currency="USD",
            max_buy_quantity=_optional_first_float(
                output,
                "max_ord_psbl_qty",
                "ovrs_max_ord_psbl_qty",
                "ord_psbl_qty",
            ),
            source="kis_overseas_stock_readonly",
        )

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol in symbols:
            if symbol.startswith("CASH"):
                prices[symbol] = 1.0
                continue
            instrument = self._instrument(symbol)
            payload = self._get(
                "/uapi/overseas-price/v1/quotations/price",
                "HHDFS00000300",
                {
                    "AUTH": "",
                    "EXCD": _quote_exchange_code(str(instrument.exchange_code or "")),
                    "SYMB": instrument.broker_symbol,
                },
            )
            output = _first_item(payload.get("output"))
            prices[symbol] = _first_float(output, "last", "ovrs_now_pric", "base", "stck_prpr")
        return prices

    def get_order_fills(self) -> list[KISOrderSummary]:
        return self._fetch_order_summaries(ccld_nccs_dvsn="01")

    def get_unfilled_orders(self) -> list[KISOrderSummary]:
        if self.config.paper_trading:
            return [
                summary
                for summary in self._fetch_order_summaries(ccld_nccs_dvsn="00")
                if summary.status
                in {
                    OrderStatus.OPEN.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                    OrderStatus.ACCEPTED_BY_BROKER.value,
                }
            ]
        payloads = self._get_pages(
            "/uapi/overseas-stock/v1/trading/inquire-nccs",
            self._tr_id(real="TTTS3018R", demo="VTTS3018R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "OVRS_EXCG_CD": "NASD",
                "SORT_SQN": "DS",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        return [
            _overseas_order_summary(row, canonical_symbol=self._canonical_symbol)
            for payload in payloads
            for row in _as_list(payload.get("output"))
        ]

    def _fetch_balance(self) -> tuple[list[KISPosition], KISCashBalance]:
        balance_payloads = self._get_pages(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            self._tr_id(real="TTTS3012R", demo="VTTS3012R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "OVRS_EXCG_CD": "NASD",
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        position_rows = [
            row for payload in balance_payloads for row in _as_list(payload.get("output2"))
        ]
        positions = [
            _overseas_position(row, canonical_symbol=self._canonical_symbol)
            for row in position_rows
            if _first_float(row, "ovrs_cblc_qty", "ord_psbl_qty", "cblc_qty13") > 0
        ]
        cash_balance = self._fetch_cash_balance()
        return positions, cash_balance

    def _fetch_cash_balance(self) -> KISCashBalance:
        payloads = self._get_pages(
            "/uapi/overseas-stock/v1/trading/inquire-present-balance",
            self._tr_id(real="CTRP6504R", demo="VTRP6504R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "WCRC_FRCR_DVSN_CD": "02",
                "NATN_CD": "840",
                "TR_MKET_CD": "00",
                "INQR_DVSN_CD": "00",
            },
        )
        cash_rows = [
            row
            for payload in payloads
            for key in ("output2", "output3")
            for row in _as_list(payload.get(key))
        ]
        merged: dict[str, Any] = {}
        for row in cash_rows:
            merged.update(row)
        cash = _first_float(
            merged,
            "frcr_use_psbl_amt",
            "frcr_dncl_amt_2",
            "tot_frcr_cblc_smtl",
            "dncl_amt",
        )
        return KISCashBalance(
            currency="USD",
            cash=cash,
            total_asset_value=_optional_first_float(
                merged,
                "tot_asst_amt",
                "frcr_evlu_tota",
                "tot_frcr_cblc_smtl",
            ),
            withdrawable_cash=_optional_first_float(
                merged,
                "frcr_drwg_psbl_amt_1",
                "nxdy_frcr_drwg_psbl_amt",
                "wdrw_psbl_tot_amt",
            ),
        )

    def _fetch_order_summaries(
        self,
        ccld_nccs_dvsn: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[KISOrderSummary]:
        if start_date is None or end_date is None:
            start_date, end_date = _default_order_date_range()
        payloads = self._get_pages(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            self._tr_id(real="TTTS3035R", demo="VTTS3035R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "PDNO": "" if self.config.paper_trading else "%",
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": "00",
                "CCLD_NCCS_DVSN": ccld_nccs_dvsn,
                "OVRS_EXCG_CD": "" if self.config.paper_trading else "NASD",
                "SORT_SQN": "DS",
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "CTX_AREA_NK200": "",
                "CTX_AREA_FK200": "",
            },
        )
        return [
            _overseas_order_summary(row, canonical_symbol=self._canonical_symbol)
            for payload in payloads
            for row in _as_list(payload.get("output"))
        ]

    def _get_pages(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
        *,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        return super()._get_pages(
            path,
            tr_id,
            params,
            fk_key="CTX_AREA_FK200",
            nk_key="CTX_AREA_NK200",
            response_fk_key="ctx_area_fk200",
            response_nk_key="ctx_area_nk200",
            max_pages=max_pages,
        )

    def _buying_power_symbol(self, symbol: str | None) -> tuple[str, str]:
        if symbol is not None:
            instrument = self._instrument(symbol)
            return instrument.broker_symbol, str(instrument.exchange_code or "NASD")
        for instrument in self.instruments.values():
            if instrument.asset_type.value != "cash":
                return instrument.broker_symbol, str(instrument.exchange_code or "NASD")
        raise ValueError(
            "KIS overseas buying power requires at least one non-cash universe instrument"
        )

    def _instrument(self, symbol: str) -> TradableInstrument:
        instrument = self.instruments.get(symbol)
        if instrument is None:
            raise ValueError(
                f"KIS overseas read-only requires universe metadata for canonical symbol: {symbol}"
            )
        if instrument.broker_product != BrokerProduct.KIS_OVERSEAS_STOCK:
            raise ValueError(f"KIS overseas read-only cannot use non-overseas instrument: {symbol}")
        if instrument.exchange_code is None:
            raise ValueError(f"KIS overseas read-only requires exchange_code for symbol: {symbol}")
        return instrument


def _default_order_date_range() -> tuple[str, str]:
    today = utc_now().astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    return today, today


def overseas_order_status_date_range(submitted_at: str) -> tuple[str, str]:
    submitted = _parse_submitted_at(submitted_at)
    now = utc_now().astimezone(ZoneInfo("America/New_York"))
    submitted_date = submitted.astimezone(ZoneInfo("America/New_York")).date()
    now_date = now.date()
    start = min(submitted_date, now_date).strftime("%Y%m%d")
    end = max(submitted_date, now_date).strftime("%Y%m%d")
    return start, end


def _parse_submitted_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return utc_now()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=utc_now().tzinfo)
    return parsed


def _format_kis_decimal(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


__all__ = ["KISRestOverseasStockReadOnlyClient"]
