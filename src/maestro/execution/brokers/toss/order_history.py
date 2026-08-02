from datetime import date
from typing import Any, Literal


def list_toss_orders(
    transport: Any,
    account_seq: int,
    *,
    status: Literal["OPEN", "CLOSED"],
    symbol: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[dict[str, Any]]:
    """Return all Toss orders for the requested lifecycle group."""

    params: dict[str, Any] = {"status": status}
    if symbol:
        params["symbol"] = symbol
    if from_date is not None:
        params["from"] = from_date.isoformat()
    if to_date is not None:
        params["to"] = to_date.isoformat()
    if status == "CLOSED":
        params["limit"] = 100

    output: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        response = transport.get(
            "/api/v1/orders",
            page_params,
            account_seq=account_seq,
        )
        result = response.get("result") if isinstance(response, dict) else None
        result = result if isinstance(result, dict) else {}
        output.extend(item for item in result.get("orders", []) if isinstance(item, dict))
        if status != "CLOSED" or result.get("hasNext") is not True:
            return output
        next_cursor = str(result.get("nextCursor") or "")
        if not next_cursor or next_cursor == cursor:
            raise ValueError("Toss closed-order pagination returned an invalid cursor")
        cursor = next_cursor


__all__ = ["list_toss_orders"]
