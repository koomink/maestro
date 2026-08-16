import pytest


def _patch_starlette_testclient_anyio_portal_deadlock() -> None:
    try:
        import asyncio
        import functools
        import io
        from urllib.parse import unquote

        import fastapi.routing
        import starlette
        import starlette.concurrency
        import starlette.routing
        import starlette.testclient as testclient
    except ModuleNotFoundError:
        return

    if starlette.__version__ != "1.0.0":
        return

    async def run_in_threadpool(func, *args, **kwargs):
        if kwargs:
            func = functools.partial(func, **kwargs)
        return func(*args)

    starlette.concurrency.run_in_threadpool = run_in_threadpool
    starlette.routing.run_in_threadpool = run_in_threadpool
    fastapi.routing.run_in_threadpool = run_in_threadpool

    def handle_request(self, request):
        scheme = request.url.scheme
        netloc = request.url.netloc.decode(encoding="ascii")
        path = request.url.path
        raw_path = request.url.raw_path
        query = request.url.query.decode(encoding="ascii")
        default_port = {"http": 80, "https": 443}[scheme]
        if ":" in netloc:
            host, port_string = netloc.split(":", 1)
            port = int(port_string)
        else:
            host = netloc
            port = default_port

        headers = [] if "host" in request.headers else [(b"host", host.encode())]
        headers += [
            (key.lower().encode(), value.encode()) for key, value in request.headers.multi_items()
        ]
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": request.method,
            "path": unquote(path),
            "raw_path": raw_path.split(b"?", 1)[0],
            "root_path": self.root_path,
            "scheme": scheme,
            "query_string": query.encode(),
            "headers": headers,
            "client": self.client,
            "server": [host, port],
            "extensions": {"http.response.debug": {}},
            "state": self.app_state.copy(),
        }

        request_complete = False
        response_started = False
        response_complete = False
        raw_kwargs = {"stream": io.BytesIO()}
        template = None
        context = None

        async def receive():
            nonlocal request_complete
            if request_complete:
                return {"type": "http.disconnect"}
            request_complete = True
            body = request.read()
            if isinstance(body, str):
                body = body.encode("utf-8")
            return {"type": "http.request", "body": body or b""}

        async def send(message):
            nonlocal context, response_complete, response_started, template
            if message["type"] == "http.response.start":
                raw_kwargs["status_code"] = message["status"]
                raw_kwargs["headers"] = [
                    (key.decode(), value.decode()) for key, value in message.get("headers", [])
                ]
                response_started = True
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                more_body = message.get("more_body", False)
                if request.method != "HEAD":
                    raw_kwargs["stream"].write(body)
                if not more_body:
                    raw_kwargs["stream"].seek(0)
                    response_complete = True
            elif message["type"] == "http.response.debug":
                template = message["info"]["template"]
                context = message["info"]["context"]

        try:
            asyncio.run(self.app(scope, receive, send))
        except BaseException as exc:
            if self.raise_server_exceptions:
                raise exc

        if self.raise_server_exceptions:
            assert response_started and response_complete, (
                "TestClient did not receive any response."
            )
        elif not response_started:
            raw_kwargs = {"status_code": 500, "headers": [], "stream": io.BytesIO()}

        raw_kwargs["stream"] = testclient.httpx.ByteStream(raw_kwargs["stream"].read())
        response = testclient.httpx.Response(**raw_kwargs, request=request)
        if template is not None:
            response.template = template
            response.context = context
        return response

    testclient._TestClientTransport.handle_request = handle_request


_patch_starlette_testclient_anyio_portal_deadlock()


@pytest.fixture(autouse=True)
def _isolate_default_operator_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MAESTRO_ENV_FILE", str(tmp_path / "missing_maestro.env"))


@pytest.fixture
def funding_orchestrator(tmp_path):
    """Build an orchestrator whose run_signal() emits a contribution funding request.

    Defaults put the ISA sleeve's cash below its min_monthly_budget, which is
    the one scenario that yields exactly one contribution_funding_request.
    Callers override the kwargs for the budget-request and fee-buffer variants.
    """
    from maestro.orchestration.orchestrator import MaestroOrchestrator
    from maestro.state.store import StateStore
    from tests.contribution_fixtures import _multi_account_config, _save_account_snapshot

    def build(
        *,
        isa_cash=1_000_000,
        ps_cash=500_000,
        isa_budget_request=False,
        fee_buffer_pct=0.0,
        isa_positions=None,
        ps_positions=None,
    ):
        config = _multi_account_config(
            tmp_path,
            isa_cash=isa_cash,
            ps_cash=ps_cash,
            isa_budget_request=isa_budget_request,
            fee_buffer_pct=fee_buffer_pct,
        )
        store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
        _save_account_snapshot(store, "kis_ps", cash=ps_cash, positions=ps_positions or [])
        _save_account_snapshot(store, "kis_isa", cash=isa_cash, positions=isa_positions or [])
        return MaestroOrchestrator(config), store

    return build
