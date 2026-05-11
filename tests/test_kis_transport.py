import io
from urllib.error import HTTPError

import pytest

from maestro.execution.brokers.kis import transport as transport_module
from maestro.execution.brokers.kis.transport import UrlLibKISTransport


class _FakeResponse:
    def __init__(self, body: str = '{"rt_cd":"0"}') -> None:
        self._body = body.encode("utf-8")
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int) -> HTTPError:
    return HTTPError(
        url="https://example.test",
        code=code,
        msg="error",
        hdrs={},
        fp=io.BytesIO(b'{"rt_cd":"1"}'),
    )


def test_kis_transport_retries_retryable_get_http_errors(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.get_method(), timeout))
        if len(calls) == 1:
            raise _http_error(500)
        return _FakeResponse('{"rt_cd":"0","output":{"ok":true}}')

    monkeypatch.setattr(transport_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(transport_module.time, "sleep", lambda seconds: None)

    payload = UrlLibKISTransport().request(
        "GET",
        "https://example.test/path",
        headers={},
        timeout_seconds=3,
    )

    assert payload["output"] == {"ok": True}
    assert calls == [("GET", 3), ("GET", 3)]


def test_kis_transport_does_not_retry_post_http_errors(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.get_method())
        raise _http_error(500)

    monkeypatch.setattr(transport_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(transport_module.time, "sleep", lambda seconds: None)

    with pytest.raises(ValueError, match="KIS request failed with HTTP 500"):
        UrlLibKISTransport().request(
            "POST",
            "https://example.test/path",
            headers={},
            json_body={"submit": True},
        )

    assert calls == ["POST"]
