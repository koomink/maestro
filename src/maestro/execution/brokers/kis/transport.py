import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class UrlLibKISTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        target_url = url
        if params:
            target_url = f"{url}?{urlencode(params)}"
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = Request(target_url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_text = response.read().decode("utf-8")
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            raise ValueError(f"KIS request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise ValueError("KIS request failed before receiving a response") from exc
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ValueError("KIS response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("KIS response JSON was not an object")
        if response_headers:
            payload["__headers__"] = response_headers
        return payload
