from typing import Any


def prices_from_bundle(data_bundle) -> dict[str, float]:
    prices = {}
    for symbol, payload in data_bundle.data.items():
        if isinstance(payload, dict) and "price" in payload:
            prices[symbol] = float(payload["price"])
        elif isinstance(payload, dict) and isinstance(payload.get("latest_price"), dict):
            prices[symbol] = float(payload["latest_price"]["price"])
        elif getattr(payload, "latest_price", None) is not None:
            prices[symbol] = float(payload.latest_price.price)
    return prices


def data_quality_issues(data_bundle) -> list[dict[str, Any]]:
    issues = []
    for request in data_bundle.requests:
        payload = data_bundle.data.get(request.symbol)
        if not isinstance(payload, dict):
            issues.append(
                {
                    "symbol": request.symbol,
                    "data_type": request.data_type,
                    "source": data_bundle.source,
                    "timestamp": None,
                    "reason": "missing_payload",
                }
            )
            continue
        latest_price = payload.get("latest_price")
        timestamp = None
        source = data_bundle.source
        if isinstance(latest_price, dict):
            timestamp = latest_price.get("timestamp")
            source = latest_price.get("source") or source
        if payload.get("is_stale"):
            issues.append(
                {
                    "symbol": request.symbol,
                    "data_type": request.data_type,
                    "source": source,
                    "timestamp": timestamp,
                    "reason": "stale",
                }
            )
        if request.data_type == "price" and latest_price is None:
            issues.append(
                {
                    "symbol": request.symbol,
                    "data_type": request.data_type,
                    "source": source,
                    "timestamp": timestamp,
                    "reason": "missing_latest_price",
                }
            )
    return issues
