from datetime import date
from typing import Any

from maestro.core.clock import utc_now
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


class KISRestDomesticStockReadOnlyClient(KISRestBaseClient, KISReadOnlyClient):
    request_error_context = "KIS read-only request"
    pagination_error_context = "KIS domestic read-only pagination"

    def get_account_snapshot(self) -> KISAccountSnapshot:
        positions, cash_balance, daily_pnl = self._fetch_balance()
        buying_power = self.get_buying_power()
        return KISAccountSnapshot(
            account_id=self.credentials.account_id,
            cash=cash_balance.cash,
            cash_by_currency={"KRW": cash_balance.cash},
            buying_power=buying_power.cash_buying_power,
            positions=positions,
            cash_balance=cash_balance,
            buying_power_detail=buying_power,
            ledger_cash_by_currency={"KRW": cash_balance.cash},
            buying_power_by_currency={"KRW": buying_power.cash_buying_power},
            daily_pnl=daily_pnl,
            daily_pnl_by_currency={"KRW": daily_pnl} if daily_pnl is not None else None,
            fetched_at=utc_now(),
            source="kis_rest_readonly",
        )

    def get_positions(self) -> list[KISPosition]:
        positions, _, _ = self._fetch_balance()
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
                "ORD_DVSN": "00",
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

    def _fetch_balance(self) -> tuple[list[KISPosition], KISCashBalance, float | None]:
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
                symbol=self._canonical_symbol(str(row.get("pdno") or row.get("prdt_code") or "")),
                name=_optional_str(row.get("prdt_name")),
                quantity=_first_float(row, "hldg_qty", "ord_psbl_qty"),
                average_price=_first_float(row, "pchs_avg_pric", "avg_unpr"),
                current_price=_position_current_price(row),
                currency="KRW",
                unrealized_pnl=_optional_first_float(row, "evlu_pfls_amt", "evlu_pfls_rt"),
            )
            for row in position_rows
            if _first_float(row, "hldg_qty", "ord_psbl_qty", default=0.0) > 0
        ]
        settled_cash = _optional_first_float(summary, "dnca_tot_amt")
        next_day_cash = _optional_first_float(summary, "nxdy_excc_amt")
        projected_settlement_cash = _optional_first_float(summary, "prvs_rcdl_excc_amt")
        # Maestro accounts on trade-date economics. KIS's D+2 projected cash
        # already includes unsettled buys/sells and transaction costs, whereas
        # dnca_tot_amt remains unchanged until settlement and creates a false
        # cash jump on D+2.
        cash = next(
            (
                value
                for value in (
                    projected_settlement_cash,
                    next_day_cash,
                    settled_cash,
                )
                if value is not None
            ),
            0.0,
        )
        cash_balance = KISCashBalance(
            cash=cash,
            total_asset_value=_optional_first_float(summary, "tot_evlu_amt", "nass_amt"),
            withdrawable_cash=settled_cash,
            settled_cash=settled_cash,
            next_day_cash=next_day_cash,
            projected_settlement_cash=projected_settlement_cash,
            transaction_costs_today=_optional_first_float(summary, "thdt_tlex_amt"),
        )
        # asst_icdc_amt (자산증감액) is today's total-asset change versus the
        # previous trading day — the closest daily-PnL figure the KIS balance
        # API reports at account level (KRW).
        daily_pnl = _optional_first_float(summary, "asst_icdc_amt")
        return positions, cash_balance, daily_pnl

    def _fetch_order_summaries(self, ccld_dvsn: str) -> list[KISOrderSummary]:
        today = date.today().strftime("%Y%m%d")
        payloads = self._get_pages(
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
        return [
            self._canonical_order_summary(_order_summary(row))
            for payload in payloads
            for row in _as_list(payload.get("output1"))
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
            fk_key="CTX_AREA_FK100",
            nk_key="CTX_AREA_NK100",
            response_fk_key="ctx_area_fk100",
            response_nk_key="ctx_area_nk100",
            max_pages=max_pages,
        )


__all__ = ["KISRestDomesticStockReadOnlyClient"]
