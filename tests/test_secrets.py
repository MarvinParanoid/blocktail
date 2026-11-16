"""The provider key never leaves the process (audit finding D-01).

The key lives in the provider URL (``…/v2/<key>``). httpx puts that URL into the
text of its exceptions, and the sync loop used to store that text as
``last_error`` — which the status bar renders on every page and
``/api/status`` returns. These tests drive the real provider through
``httpx.MockTransport`` (and the fake one for generic failures) and look for
the key everywhere a reader or an operator could see it: the page, the status
fragment, the JSON API, the stored row and the log.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chains.ethereum.alchemy import AlchemyProvider, ProviderError, build_url
from app.main import create_app
from app.models import Balance
from app.prices import AlchemyPrices
from tests.conftest import (
    MAIN,
    USDC,
    FakePrices,
    FakeProvider,
    make_config,
    make_settings,
    run,
)

KEY = "sEcReTkEy0123456789abcdef"
URL = build_url(KEY)
PRICES_BASE = "https://api.g.alchemy.com/prices/v1"


def leaks(text: str) -> bool:
    return KEY in text or "/v2/" + KEY[:6] in text


def keyed_settings(tmp_path, **overrides):
    values = {"alchemy_api_key": KEY, "alchemy_url": URL, "prices_base_url": PRICES_BASE}
    values.update(overrides)
    return make_settings(tmp_path, **values)


def alchemy(handler, **kwargs) -> AlchemyProvider:
    kwargs.setdefault("max_retries", 1)
    return AlchemyProvider(
        URL, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
    )


def stored_error(ctx) -> str:
    """The raw column, not the read path, so a redaction applied only while
    rendering does not hide a key that is sitting in the database."""
    row = ctx.db.connect().execute(
        "SELECT last_error FROM sync_state WHERE chain_id = 'ethereum'"
    ).fetchone()
    return (row[0] if row else None) or ""


def surfaces(client) -> dict[str, str]:
    return {
        "/": client.get("/").text,
        "/status": client.get("/status").text,
        "/api/status": client.get("/api/status").text,
        "/healthz": client.get("/healthz").text,
    }


def run_failing_cycle(tmp_path, provider, *, price_source=None, **overrides):
    app = create_app(
        settings=keyed_settings(tmp_path, **overrides),
        config=make_config(),
        provider=provider,
        price_source=price_source or FakePrices(),
        run_indexer=False,
    )
    client = TestClient(app)
    client.__enter__()
    run(app.state.ctx.indexer.run_once())
    return app, client


@pytest.fixture
def debug_log(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


# ------------------------------------------------------- HTTP status codes


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_rejected_request_does_not_publish_the_key(tmp_path, debug_log, status):
    provider = alchemy(lambda request: httpx.Response(status, json={"error": "no"}))
    app, client = run_failing_cycle(tmp_path, provider)
    try:
        for path, body in surfaces(client).items():
            assert not leaks(body), f"key rendered by {path} after HTTP {status}"
        stored = stored_error(app.state.ctx)
        assert not leaks(stored)
        assert not leaks(debug_log.text)

        # What an operator needs to act on is still there.
        assert "eth_blockNumber" in stored
        assert str(status) in stored
        assert client.get("/api/status").json()["state"] == "error"
    finally:
        client.__exit__(None, None, None)


def test_a_rejected_key_says_what_to_check(tmp_path):
    provider = alchemy(lambda request: httpx.Response(401, json={"error": "no"}))
    app, client = run_failing_cycle(tmp_path, provider)
    try:
        assert "ALCHEMY_API_KEY" in stored_error(app.state.ctx)
    finally:
        client.__exit__(None, None, None)


# ------------------------------------------------- network errors, timeouts


@pytest.mark.parametrize(
    "error, name",
    [
        (httpx.ConnectError(f"[Errno 111] Connection refused: {URL}"), "ConnectError"),
        (httpx.ReadTimeout(f"timed out reading {URL}"), "ReadTimeout"),
        (httpx.ConnectTimeout(f"timed out connecting to {URL}"), "ConnectTimeout"),
        (httpx.RemoteProtocolError(f"peer closed connection: {URL}"), "RemoteProtocolError"),
    ],
)
def test_a_network_failure_does_not_publish_the_key(tmp_path, debug_log, error, name):
    def handler(request):
        raise error

    app, client = run_failing_cycle(tmp_path, alchemy(handler))
    try:
        for path, body in surfaces(client).items():
            assert not leaks(body), f"key rendered by {path} after {name}"
        stored = stored_error(app.state.ctx)
        assert not leaks(stored)
        assert not leaks(debug_log.text)
        assert "eth_blockNumber" in stored and name in stored
    finally:
        client.__exit__(None, None, None)


def test_retries_keep_the_diagnosis_and_drop_the_url(tmp_path, debug_log):
    provider = alchemy(lambda request: httpx.Response(404), max_retries=2)
    app, client = run_failing_cycle(tmp_path, provider)
    try:
        stored = stored_error(app.state.ctx)
        assert not leaks(stored) and "http" not in stored.lower().replace("http 404", "")
        assert "after 2 attempts" in stored
    finally:
        client.__exit__(None, None, None)


# ----------------------------------------- the key inside an exception text


@pytest.mark.parametrize("where", ["fail_with", "fail_transfers_with", "fail_balances_with"])
def test_a_key_inside_any_exception_text_is_not_stored(tmp_path, debug_log, where):
    """Whatever a provider raises, the boundary that stores and logs it does
    not trust it to be clean."""
    provider = FakeProvider(native_balances={MAIN: 1})
    setattr(provider, where, RuntimeError(f"upstream refused {URL} (key {KEY})"))
    app, client = run_failing_cycle(tmp_path, provider)
    try:
        for path, body in surfaces(client).items():
            assert not leaks(body), f"key rendered by {path} via {where}"
        stored = stored_error(app.state.ctx)
        assert stored, "the failure must still be recorded"
        assert "upstream refused" in stored
        assert not leaks(stored)
        assert not leaks(debug_log.text)
    finally:
        client.__exit__(None, None, None)


def test_a_key_already_in_the_database_is_not_rendered(tmp_path):
    """An instance upgraded from a leaking version may have the key sitting in
    last_error until the next successful sync clears it."""
    app = create_app(
        settings=keyed_settings(tmp_path), config=make_config(),
        provider=FakeProvider(), price_source=FakePrices(), run_indexer=False,
    )
    with TestClient(app) as client:
        app.state.ctx.db.record_attempt("ethereum")
        app.state.ctx.db.connect().execute(
            "UPDATE sync_state SET last_error = ? WHERE chain_id = 'ethereum'",
            (f"eth_blockNumber failed after 5 attempts: Client error '401' for url '{URL}'",),
        )
        for path, body in surfaces(client).items():
            assert not leaks(body), f"stored key rendered by {path}"
        assert "eth_blockNumber" in client.get("/api/status").json()["last_error"]


# ------------------------------------------------------------- the log


def test_a_failing_price_source_does_not_log_the_key(tmp_path, debug_log):
    prices = AlchemyPrices(
        KEY, base_url=PRICES_BASE, max_retries=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    provider = FakeProvider(
        native_balances={MAIN: 10**18}, token_balances={MAIN: [Balance(USDC, 10**6)]}
    )
    _, client = run_failing_cycle(tmp_path, provider, price_source=prices)
    try:
        assert "price refresh failed" in debug_log.text
        assert not leaks(debug_log.text)
        assert "401" in debug_log.text
    finally:
        client.__exit__(None, None, None)


def test_a_failing_metadata_lookup_does_not_log_the_key(debug_log):
    def handler(request):
        if b"alchemy_getTokenBalances" in request.content:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                # Not a listed token: a listed one needs no lookup to fail.
                "tokenBalances": [{"contractAddress": "0x" + "c0ffee" * 6 + "c0ff",
                                   "tokenBalance": "0x1"}]
            }})
        return httpx.Response(404)

    provider = alchemy(handler)
    run(provider.get_token_balances(MAIN))
    run(provider.close())
    # The provider's own record, checked without relying on the log scrubber:
    # the message must be clean where it is made. (httpx's request log is
    # silenced by the app's logging setup and scrubbed by the factory.)
    own = [r for r in debug_log.records if r.name == "app.chains.ethereum.alchemy"]
    assert own and "token metadata lookup failed" in own[0].getMessage()
    assert all(not leaks(r.getMessage()) for r in own)
    assert "HTTP 404" in own[0].getMessage()


def test_any_log_line_is_scrubbed_once_the_app_is_built(tmp_path, debug_log):
    """Defence in depth: a log call written next year, with the URL in it, is
    still caught at the boundary — message, arguments and traceback alike."""
    create_app(
        settings=keyed_settings(tmp_path), config=make_config(),
        provider=FakeProvider(), price_source=FakePrices(), run_indexer=False,
    )
    log = logging.getLogger("app.somewhere.new")
    log.warning("calling %s", URL)
    log.error(f"inline {KEY}")
    try:
        raise httpx.ConnectError(f"cannot reach {URL}")
    except httpx.ConnectError:
        log.exception("unexpected error")

    assert "calling" in debug_log.text and "unexpected error" in debug_log.text
    assert "ConnectError" in debug_log.text, "the traceback is kept, only scrubbed"
    assert not leaks(debug_log.text)
    for record in debug_log.records:
        assert not leaks(record.getMessage())


# --------------------------------------- each layer, independently of the other


@pytest.mark.parametrize(
    "respond, expected",
    [
        (lambda r: httpx.Response(400), "HTTP 400"),
        (lambda r: httpx.Response(401), "HTTP 401"),
        (lambda r: httpx.Response(403), "HTTP 403"),
        (lambda r: httpx.Response(404), "HTTP 404"),
        (lambda r: httpx.Response(200, content=b"<html>not json"), "unreadable response"),
    ],
)
def test_provider_errors_are_clean_where_they_are_made(respond, expected):
    """No scrubber involved: the message itself never quotes httpx."""
    from app.chains.ethereum.alchemy import ProviderError

    provider = alchemy(respond)
    with pytest.raises(ProviderError) as caught:
        run(provider.get_native_balance(MAIN))
    run(provider.close())
    text = str(caught.value)
    assert expected in text and "eth_getBalance" in text
    assert "://" not in text and KEY not in text


def test_price_errors_are_clean_where_they_are_made():
    from app.prices import PriceError

    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    prices = AlchemyPrices(
        KEY, base_url=PRICES_BASE, max_retries=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PriceError) as caught:
        run(prices._request("GET", "/tokens/by-symbol"))
    run(prices.close())
    assert "ConnectError" in str(caught.value) and "/tokens/by-symbol" in str(caught.value)
    assert "://" not in str(caught.value) and KEY not in str(caught.value)


def test_a_programming_error_is_not_swallowed_as_a_provider_failure():
    """Only transport and parsing failures are retried and described; a bug
    in the code path surfaces as itself."""
    def handler(request):
        raise ZeroDivisionError("a bug, not the network")

    provider = alchemy(handler)
    with pytest.raises(ZeroDivisionError):
        run(provider.get_head_block())
    run(provider.close())


@pytest.mark.parametrize(
    "text, gone, kept",
    [
        (f"POST {URL} failed", KEY, "POST https://eth-mainnet.g.alchemy.com/v2/***"),
        (
            "https://eth-mainnet.g.alchemy.com/v2/unregisteredKey123 down",
            "unregisteredKey123",
            "/v2/***",
        ),
        (
            "https://api.g.alchemy.com/prices/v1/unregisteredKey123/tokens",
            "unregisteredKey123",
            "/prices/v1/***/tokens",
        ),
        ("https://proxy.example/rpc?apikey=s3cr3tvalue&x=1", "s3cr3tvalue", "?apikey=***&x=1"),
        ("https://user:hunter2hunter2@proxy.example/rpc", "hunter2hunter2", "://user:***@proxy.example"),
    ],
)
def test_redact_masks_registered_and_url_shaped_secrets(text, gone, kept):
    from app.redact import redact, register_secret

    register_secret(KEY)
    out = redact(text)
    assert gone not in out
    assert kept in out


def test_redact_leaves_ordinary_text_alone():
    from app.redact import redact, register_secret

    register_secret("short")  # below the minimum: would mask ordinary words
    text = "eth_blockNumber failed after 5 attempts: HTTP 503 (short outage)"
    assert redact(text) == text
    assert redact(None) is None and redact("") == ""


def test_the_scrubber_keeps_formatters_that_read_arguments_working(tmp_path):
    """uvicorn's access log unpacks record.args positionally; collapsing the
    arguments into the message would break every request line."""
    from uvicorn.logging import AccessFormatter

    create_app(
        settings=keyed_settings(tmp_path), config=make_config(),
        provider=FakeProvider(), price_source=FakePrices(), run_indexer=False,
    )
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", "GET", f"/?q={KEY}", "1.1", 200),
        None,
    )
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert '127.0.0.1:5000 - "GET /?q=*** HTTP/1.1" 200' in line
    assert isinstance(record.args[4], int)


def test_exception_arguments_are_scrubbed(tmp_path, debug_log):
    create_app(
        settings=keyed_settings(tmp_path), config=make_config(),
        provider=FakeProvider(), price_source=FakePrices(), run_indexer=False,
    )
    logging.getLogger("app.sync").warning(
        "price refresh failed: %s: %s", "ConnectError", httpx.ConnectError(f"to {URL}")
    )
    assert "price refresh failed: ConnectError: to https://" in debug_log.text
    assert not leaks(debug_log.text)


# ------------------------------------------------------- S0 follow-up checks


@pytest.mark.parametrize("auth", [False, True])
def test_healthz_stays_200_while_the_provider_rejects_the_key(tmp_path, auth):
    """Liveness is not provider health: a rejected key must not make the
    orchestrator restart a container that is serving fine."""
    overrides = {"auth_user": "admin", "auth_password": "correct horse"} if auth else {}
    provider = alchemy(lambda request: httpx.Response(401))
    _, client = run_failing_cycle(tmp_path, provider, **overrides)
    try:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        if not auth:
            assert health.json()["sync_state"] == "error"
    finally:
        client.__exit__(None, None, None)


def counting(handler):
    calls = {"n": 0}

    def wrapped(request):
        calls["n"] += 1
        return handler(request)

    return wrapped, calls


AUTH_403_BODIES = [
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Must be authenticated!"}},
    {"error": {"message": "Unspecified origin not on whitelist."}},
    {"error": {"message": "ETH_MAINNET is not enabled for this app."}},
]


def test_a_401_is_not_retried():
    handler, calls = counting(lambda request: httpx.Response(401, json={"error": "no"}))
    provider = alchemy(handler, max_retries=4)
    with pytest.raises(ProviderError) as caught:
        run(provider.get_head_block())
    run(provider.close())
    assert calls["n"] == 1, "a rejected key does not get better by asking again"
    assert "HTTP 401" in str(caught.value) and "ALCHEMY_API_KEY" in str(caught.value)


@pytest.mark.parametrize("body", AUTH_403_BODIES)
def test_a_403_that_says_access_is_denied_is_not_retried(body):
    handler, calls = counting(lambda request: httpx.Response(403, json=body))
    provider = alchemy(handler, max_retries=4)
    with pytest.raises(ProviderError) as caught:
        run(provider.get_head_block())
    run(provider.close())
    assert calls["n"] == 1
    assert "HTTP 403" in str(caught.value) and "://" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403),
        httpx.Response(403, json={"error": {"code": 429, "message":
            "Your app has exceeded its compute units per second capacity."}}),
    ],
)
def test_a_throttling_403_is_still_retried(response, monkeypatch):
    """The existing policy: Alchemy answers a burst with 403."""
    import app.chains.ethereum.alchemy as module

    async def no_wait(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    handler, calls = counting(lambda request: response)
    provider = alchemy(handler, max_retries=3)
    with pytest.raises(ProviderError):
        run(provider.get_head_block())
    run(provider.close())
    assert calls["n"] == 3


@pytest.mark.parametrize("status, body", [(401, None), (403, AUTH_403_BODIES[0])])
def test_the_price_source_does_not_retry_a_denial(status, body):
    from app.prices import PriceError

    handler, calls = counting(lambda request: httpx.Response(status, json=body))
    prices = AlchemyPrices(
        KEY, base_url=PRICES_BASE, max_retries=3,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PriceError) as caught:
        run(prices._request("GET", "/tokens/by-symbol"))
    run(prices.close())
    assert calls["n"] == 1
    assert f"HTTP {status}" in str(caught.value) and KEY not in str(caught.value)


def test_the_price_source_still_retries_a_temporary_failure(monkeypatch):
    import app.prices as module
    from app.prices import PriceError

    async def no_wait(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    handler, calls = counting(lambda request: httpx.Response(503))
    prices = AlchemyPrices(
        KEY, base_url=PRICES_BASE, max_retries=3,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PriceError):
        run(prices._request("GET", "/tokens/by-symbol"))
    run(prices.close())
    assert calls["n"] == 3


SHORT_PASSWORD = "Zq7!x"  # below the redaction minimum on purpose


def test_settings_do_not_print_their_secrets(tmp_path):
    settings = keyed_settings(tmp_path, auth_user="admin", auth_password=SHORT_PASSWORD)
    text = repr(settings) + str(settings)
    assert SHORT_PASSWORD not in text
    assert KEY not in text


def test_a_short_basic_auth_password_appears_in_no_diagnostics(tmp_path, debug_log):
    """Short passwords are not registered with the scrubber (they would mask
    ordinary words), so the guarantee is that nothing writes one down."""
    provider = alchemy(lambda request: httpx.Response(401))
    app, client = run_failing_cycle(
        tmp_path, provider, auth_user="admin", auth_password=SHORT_PASSWORD
    )
    try:
        client.auth = ("admin", SHORT_PASSWORD)
        assert client.get("/").status_code == 200
        bodies = [client.get(path).text for path in ("/", "/status", "/api/status", "/healthz")]
        bodies.append(client.get("/", auth=("admin", "wrong")).text)
        for body in bodies:
            assert SHORT_PASSWORD not in body
        assert SHORT_PASSWORD not in stored_error(app.state.ctx)
        assert SHORT_PASSWORD not in debug_log.text
        assert SHORT_PASSWORD not in repr(app.state.ctx)
    finally:
        client.__exit__(None, None, None)
