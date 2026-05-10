from datetime import date
from typing import Any

from maestro.config.models import KISConfig
from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.auth import KISAuthManager, KISToken
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
    _tr_cont,
)
from maestro.execution.brokers.kis.transport import UrlLibKISTransport


class KISRestOverseasStockReadOnlyClient(KISReadOnlyClient):
    def __init__(
        self,
        config: KISConfig,
        *,
        transport: UrlLibKISTransport | None = None,
        auth_manager: KISAuthManager | None = None,
        instruments: list[TradableInstrument] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibKISTransport()
        self.auth_manager = auth_manager or KISAuthManager(config, self.transport)
        self.credentials = self.auth_manager.get_credentials()
        self.instruments = {instrument.symbol: instrument for instrument in instruments or []}
        self._broker_symbol_to_canonical = {
            instrument.broker_symbol: instrument.symbol for instrument in instruments or []
        }

    def get_account_snapshot(self) -> KISAccountSnapshot:
        positions, cash_balance = self._fetch_balance()
        buying_power = self.get_buying_power()
        return KISAccountSnapshot(
            account_id=self.credentials.account_id,
            cash=cash_balance.cash,
            buying_power=buying_power.cash_buying_power,
            positions=positions,
            cash_balance=cash_balance,
            buying_power_detail=buying_power,
            fetched_at=utc_now(),
            source="kis_overseas_stock_readonly",
        )

    def get_positions(self) -> list[KISPosition]:
        positions, _ = self._fetch_balance()
        return positions

    def get_buying_power(self, symbol: str | None = None) -> KISBuyingPower:
        broker_symbol, exchange_code = self._buying_power_symbol(symbol)
        payload = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            self._tr_id(real="TTTS3007R", demo="VTTS3007R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "OVRS_EXCG_CD": exchange_code,
                "OVRS_ORD_UNPR": "1",
                "ITEM_CD": broker_symbol,
            },
        )
        output = _first_item(payload.get("output"))
        return KISBuyingPower(
            symbol=symbol or self._canonical_symbol(broker_symbol),
            order_price=_optional_first_float(output, "ovrs_ord_unpr", "ord_unpr"),
            cash_buying_power=_first_float(
                output,
                "ovrs_ord_psbl_amt",
                "frcr_ord_psbl_amt1",
                "ord_psbl_amt",
                "max_ord_psbl_amt",
            ),
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
        payloads = self._get_pages(
            "/uapi/overseas-stock/v1/trading/inquire-nccs",
            "TTTS3018R",
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

    def _fetch_order_summaries(self, ccld_nccs_dvsn: str) -> list[KISOrderSummary]:
        today = date.today().strftime("%Y%m%d")
        payloads = self._get_pages(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            self._tr_id(real="TTTS3035R", demo="VTTS3035R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "PDNO": "" if self.config.paper_trading else "%",
                "ORD_STRT_DT": today,
                "ORD_END_DT": today,
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
        pages: list[dict[str, Any]] = []
        next_params = dict(params)
        tr_cont = ""
        for _ in range(max_pages):
            payload = self._get(path, tr_id, next_params, tr_cont=tr_cont)
            pages.append(payload)
            if _tr_cont(payload) not in {"M", "F"}:
                return pages
            next_params["CTX_AREA_FK200"] = str(payload.get("ctx_area_fk200") or "")
            next_params["CTX_AREA_NK200"] = str(payload.get("ctx_area_nk200") or "")
            tr_cont = "N"
        raise ValueError(f"KIS overseas read-only pagination exceeded {max_pages} pages: {path}")

    def _get(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
        *,
        tr_cont: str = "",
    ) -> dict[str, Any]:
        token = self.auth_manager.get_access_token()
        payload = self.transport.request(
            "GET",
            f"{self.config.resolved_base_url()}{path}",
            headers=self._headers(tr_id, token, tr_cont=tr_cont),
            params=params,
            timeout_seconds=self.config.timeout_seconds,
        )
        if payload.get("rt_cd") not in ("0", None):
            msg_cd = payload.get("msg_cd", "unknown")
            msg1 = payload.get("msg1", "KIS request failed")
            raise ValueError(f"KIS overseas read-only request failed: {msg_cd} {msg1}")
        return payload

    def _headers(self, tr_id: str, token: KISToken, *, tr_cont: str = "") -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "authorization": f"Bearer {token.access_token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": tr_cont,
        }

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

    def _canonical_symbol(self, broker_symbol: str) -> str:
        return self._broker_symbol_to_canonical.get(broker_symbol, broker_symbol)

    def _tr_id(self, *, real: str, demo: str) -> str:
        return demo if self.config.paper_trading else real


__all__ = ["KISRestOverseasStockReadOnlyClient"]
