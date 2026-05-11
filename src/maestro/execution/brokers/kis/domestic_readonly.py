from datetime import date
from typing import Any

from maestro.config.models import KISConfig
from maestro.core.clock import utc_now
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
    _as_dict,
    _as_list,
    _first_float,
    _first_item,
    _kis_price,
    _optional_first_float,
    _optional_float,
    _optional_str,
    _order_summary,
    _position_current_price,
)
from maestro.execution.brokers.kis.transport import UrlLibKISTransport


class KISRestDomesticStockReadOnlyClient(KISReadOnlyClient):
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

    def get_account_snapshot(self) -> KISAccountSnapshot:
        positions, cash_balance = self._fetch_balance()
        buying_power = self.get_buying_power()
        return KISAccountSnapshot(
            account_id=self.credentials.account_id,
            cash=cash_balance.cash,
            cash_by_currency={"KRW": cash_balance.cash},
            buying_power=buying_power.cash_buying_power,
            positions=positions,
            cash_balance=cash_balance,
            buying_power_detail=buying_power,
            fetched_at=utc_now(),
            source="kis_rest_readonly",
        )

    def get_positions(self) -> list[KISPosition]:
        positions, _ = self._fetch_balance()
        return positions

    def get_buying_power(
        self,
        symbol: str | None = None,
        order_price: float | None = None,
    ) -> KISBuyingPower:
        pdno = symbol or ""
        payload = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            self._tr_id(real="TTTC8908R", demo="VTTC8908R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "PDNO": pdno,
                "ORD_UNPR": _kis_price(order_price) if order_price is not None else "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        output = _as_dict(payload.get("output"))
        return KISBuyingPower(
            symbol=symbol,
            order_price=_optional_float(output.get("ord_unpr")),
            cash_buying_power=_first_float(output, "nrcvb_buy_amt", "max_buy_amt", "ord_psbl_cash"),
            max_buy_quantity=_optional_first_float(output, "nrcvb_buy_qty", "max_buy_qty"),
            source="kis_rest_readonly",
        )

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol in symbols:
            if symbol.startswith("CASH"):
                prices[symbol] = 1.0
                continue
            payload = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "FHKST01010100",
                {
                    "FID_COND_MRKT_DIV_CODE": self.config.quote_market_code,
                    "FID_INPUT_ISCD": self._broker_symbol(symbol),
                },
            )
            output = _as_dict(payload.get("output"))
            prices[symbol] = _first_float(output, "stck_prpr", "ovrs_now_pric", "last")
        return prices

    def get_order_fills(self) -> list[KISOrderSummary]:
        return self._fetch_order_summaries(ccld_dvsn="00")

    def get_unfilled_orders(self) -> list[KISOrderSummary]:
        return self._fetch_order_summaries(ccld_dvsn="02")

    def _fetch_balance(self) -> tuple[list[KISPosition], KISCashBalance]:
        payload = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            self._tr_id(real="TTTC8434R", demo="VTTC8434R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        position_rows = _as_list(payload.get("output1"))
        summary = _first_item(payload.get("output2"))
        positions = [
            KISPosition(
                symbol=str(row.get("pdno") or row.get("prdt_code") or ""),
                name=_optional_str(row.get("prdt_name")),
                quantity=_first_float(row, "hldg_qty", "ord_psbl_qty"),
                average_price=_first_float(row, "pchs_avg_pric", "avg_unpr"),
                current_price=_position_current_price(row),
                unrealized_pnl=_optional_first_float(row, "evlu_pfls_amt", "evlu_pfls_rt"),
            )
            for row in position_rows
            if _first_float(row, "hldg_qty", "ord_psbl_qty", default=0.0) > 0
        ]
        cash = _first_float(summary, "dnca_tot_amt", "prvs_rcdl_excc_amt", "nxdy_excc_amt")
        cash_balance = KISCashBalance(
            cash=cash,
            total_asset_value=_optional_first_float(summary, "tot_evlu_amt", "nass_amt"),
            withdrawable_cash=_optional_first_float(summary, "dnca_tot_amt", "nxdy_excc_amt"),
        )
        return positions, cash_balance

    def _fetch_order_summaries(self, ccld_dvsn: str) -> list[KISOrderSummary]:
        today = date.today().strftime("%Y%m%d")
        payload = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            self._tr_id(real="TTTC0081R", demo="VTTC0081R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": "",
                "CCLD_DVSN": ccld_dvsn,
                "INQR_DVSN": "00",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "EXCG_ID_DVSN_CD": "KRX",
            },
        )
        return [_order_summary(row) for row in _as_list(payload.get("output1"))]

    def _get(self, path: str, tr_id: str, params: dict[str, str]) -> dict[str, Any]:
        token = self.auth_manager.get_access_token()
        payload = self.transport.request(
            "GET",
            f"{self.config.resolved_base_url()}{path}",
            headers=self._headers(tr_id, token),
            params=params,
            timeout_seconds=self.config.timeout_seconds,
        )
        if payload.get("rt_cd") not in ("0", None):
            msg_cd = payload.get("msg_cd", "unknown")
            msg1 = payload.get("msg1", "KIS request failed")
            raise ValueError(f"KIS read-only request failed: {msg_cd} {msg1}")
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

    def _tr_id(self, *, real: str, demo: str) -> str:
        return demo if self.config.paper_trading else real

    def _broker_symbol(self, canonical_symbol: str) -> str:
        instrument = self.instruments.get(canonical_symbol)
        return instrument.broker_symbol if instrument else canonical_symbol


__all__ = ["KISRestDomesticStockReadOnlyClient"]
