from typing import Any

from maestro.config.models import KISConfig
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.auth import KISAuthManager, KISToken
from maestro.execution.brokers.kis.models import KISOrderSummary
from maestro.execution.brokers.kis.parsers import _tr_cont
from maestro.execution.brokers.kis.transport import UrlLibKISTransport


class KISRestBaseClient:
    request_error_context = "KIS request"
    post_error_context = "KIS request"
    pagination_error_context = "KIS pagination"

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

    def _get(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
        *,
        tr_cont: str = "",
        error_context: str | None = None,
    ) -> dict[str, Any]:
        token = self.auth_manager.get_access_token()
        payload = self.transport.request(
            "GET",
            f"{self.config.resolved_base_url()}{path}",
            headers=self._headers(tr_id, token, tr_cont=tr_cont),
            params=params,
            timeout_seconds=self.config.timeout_seconds,
        )
        self._raise_for_error(payload, error_context or self.request_error_context)
        return payload

    def _post(
        self,
        path: str,
        tr_id: str,
        json_body: dict[str, str],
        *,
        error_context: str | None = None,
    ) -> dict[str, Any]:
        token = self.auth_manager.get_access_token()
        payload = self.transport.request(
            "POST",
            f"{self.config.resolved_base_url()}{path}",
            headers=self._headers(tr_id, token),
            json_body=json_body,
            timeout_seconds=self.config.timeout_seconds,
        )
        self._raise_for_error(payload, error_context or self.post_error_context)
        return payload

    def _get_pages(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
        *,
        fk_key: str,
        nk_key: str,
        response_fk_key: str,
        response_nk_key: str,
        max_pages: int = 10,
        error_context: str | None = None,
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        next_params = dict(params)
        tr_cont = ""
        for _ in range(max_pages):
            payload = self._get(path, tr_id, next_params, tr_cont=tr_cont)
            pages.append(payload)
            if _tr_cont(payload) not in {"M", "F"}:
                return pages
            next_params[fk_key] = str(payload.get(response_fk_key) or "")
            next_params[nk_key] = str(payload.get(response_nk_key) or "")
            tr_cont = "N"
        context = error_context or self.pagination_error_context
        raise ValueError(f"{context} exceeded {max_pages} pages: {path}")

    def _headers(self, tr_id: str, token: KISToken, *, tr_cont: str = "") -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/plain",
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

    def _canonical_symbol(self, broker_symbol: str) -> str:
        return self._broker_symbol_to_canonical.get(broker_symbol, broker_symbol)

    def _canonical_order_summary(self, summary: KISOrderSummary) -> KISOrderSummary:
        return summary.model_copy(update={"symbol": self._canonical_symbol(summary.symbol)})

    def _raise_for_error(self, payload: dict[str, Any], context: str) -> None:
        if payload.get("rt_cd") not in ("0", None):
            msg_cd = payload.get("msg_cd", "unknown")
            msg1 = payload.get("msg1", "KIS request failed")
            raise ValueError(f"{context} failed: {msg_cd} {msg1}")


__all__ = ["KISRestBaseClient"]
