import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener


class TossTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.data = data or {}


class TossRateLimitError(TossTransportError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        rate_limit_remaining: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limit_remaining = rate_limit_remaining


class TossRestTransport:
    def __init__(
        self,
        *,
        base_url: str,
        access_token_provider: Callable[[], str],
        timeout_seconds: float,
        opener: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token_provider = access_token_provider
        self.timeout_seconds = timeout_seconds
        self.opener = opener or build_opener()

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        account_seq: int | None = None,
    ) -> dict[str, Any]:
        query = urlencode(params or {})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        return self._request("GET", url, account_seq=account_seq)

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        account_seq: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"{self.base_url}{path}",
            account_seq=account_seq,
            payload=payload or {},
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        account_seq: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.access_token_provider()}"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except HTTPError as exc:
            if exc.code == 429:
                raise TossRateLimitError(
                    "Toss OpenAPI rate limit exceeded",
                    retry_after=_optional_int(exc.headers.get("Retry-After")),
                    rate_limit_remaining=_optional_int(
                        exc.headers.get("X-RateLimit-Remaining")
                    ),
                ) from exc
            error_payload = _http_error_payload(exc)
            error = error_payload.get("error") if isinstance(error_payload, dict) else None
            error = error if isinstance(error, dict) else {}
            raise TossTransportError(
                str(error.get("message") or f"Toss OpenAPI request failed: HTTP {exc.code}"),
                status_code=exc.code,
                error_code=_optional_str(error.get("code")),
                request_id=_optional_str(error.get("requestId")),
                data=error.get("data") if isinstance(error.get("data"), dict) else None,
            ) from exc
        except URLError as exc:
            raise TossTransportError(f"Toss OpenAPI request failed: {exc.reason}") from exc
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _http_error_payload(exc: HTTPError) -> dict[str, Any]:
    try:
        body = exc.read()
    except Exception:
        return {}
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["TossRateLimitError", "TossRestTransport", "TossTransportError"]
