"""HTTP hardening (S8a): what a browser may ask of this app, and what every
answer carries.

The app has one kind of credential: HTTP basic auth, which a browser attaches
by itself to any request for this origin — including one another site makes it
send. So a request is judged by where it came from, as the browser reports it:

* a write (anything but GET/HEAD) is accepted from this page only:
  `Sec-Fetch-Site: same-origin` (or `none`, a request the user typed); with no
  fetch metadata at all — an old browser, curl — only with `HX-Request: true`,
  a header no other site can make a browser send without a CORS preflight this
  app never grants;
* a read from another site is accepted as a top-level navigation only (a link
  to the app), never as an embedded image, script, frame or fetch.

Every response — page, fragment, JSON, static file, error — carries the same
security headers; nothing private is cacheable.
"""

from __future__ import annotations

import base64
import logging
import re
import sqlite3
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import STRANGER, address, make_settings, run

EVIL = address("evil")

# What a current browser sends with a request the page makes through htmx.
HTMX = {
    "hx-request": "true",
    "hx-current-url": "http://testserver/",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "origin": "http://testserver",
}
# The same page posting a plain form (no script): a navigation.
FORM = {
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "origin": "http://testserver",
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


def _client(tmp_path, watch_config, provider, prices, sample_transfers, **overrides):
    provider.transfers = sample_transfers
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings=settings, config=watch_config, provider=provider,
                     price_source=prices, run_indexer=False)
    return app


@pytest.fixture
def client(tmp_path, watch_config, provider, prices, sample_transfers):
    app = _client(tmp_path, watch_config, provider, prices, sample_transfers)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        run(app.state.ctx.indexer.run_once())
        ctx = app.state.ctx
        # A wallet added from the page: the only kind the page may change.
        ctx.db.add_account(chain_id="ethereum", address=address("ui"), name="Ui", is_owned=False)
        test_client.ctx = ctx
        yield test_client


def ui_wallet(client) -> int:
    return next(a.id for a in client.ctx.db.list_accounts() if a.name == "Ui")


def state(client) -> list[str]:
    """Everything the database holds, as SQL. A read must leave it as it was."""
    with sqlite3.connect(client.ctx.settings.database_path) as conn:
        return list(conn.iterdump())


# Every route that changes something, with a body that would change it.
WRITES = {
    "add wallet": lambda c: ("/wallets", {"name": "Evil", "address": EVIL, "chain": "ethereum"}),
    "rename wallet": lambda c: (f"/wallets/{ui_wallet(c)}", {"name": "Evil"}),
    "move wallet": lambda c: (f"/wallets/{ui_wallet(c)}", {"set_owned": "true"}),
    "remove wallet": lambda c: (f"/wallets/{ui_wallet(c)}/remove", {}),
    "older history": lambda c: ("/history/older", {}),
}


def post(client, write: str, headers: dict):
    path, data = WRITES[write](client)
    return client.post(path, data=data, headers=headers)


# ------------------------------------------------------------------ CSRF


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_write_from_another_site_is_refused(client, write, site):
    """`same-site` is another site too: any subdomain of the same domain — a
    blog, a preview deploy — is not this app (D-11)."""
    before = state(client)
    response = post(client, write, {**HTMX, "sec-fetch-site": site,
                                     "origin": "https://blog.example.com"})
    assert response.status_code == 403
    assert state(client) == before


@pytest.mark.parametrize("write", WRITES)
def test_a_write_through_htmx_from_this_page_is_accepted(client, write):
    before = state(client)
    response = post(client, write, HTMX)
    assert response.status_code == 200, response.text
    assert state(client) != before


@pytest.mark.parametrize("write", WRITES)
def test_a_plain_form_post_from_this_page_is_accepted(client, write):
    """Without htmx — the form's own action, no script: the browser still says
    where it came from."""
    response = post(client, write, FORM)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("write", WRITES)
def test_a_write_without_any_proof_of_origin_is_refused(client, write):
    """No fetch metadata and no htmx header: nothing says this page sent it.
    An old browser posting a form from another site looks exactly like this."""
    before = state(client)
    assert post(client, write, {}).status_code == 403
    assert post(client, write, {"origin": "http://testserver"}).status_code == 403
    assert post(client, write, {"referer": "http://testserver/"}).status_code == 403
    assert state(client) == before


def test_a_client_without_fetch_metadata_proves_itself_with_the_htmx_header(client):
    """curl, a script, an old browser running the page's own htmx: a custom
    header cannot be added to a cross-origin request without a preflight."""
    assert post(client, "older history", {"hx-request": "true"}).status_code == 200


def test_a_null_origin_without_fetch_metadata_is_refused(client):
    """A sandboxed frame or a data: URL posts with `Origin: null`."""
    assert post(client, "add wallet", {"origin": "null"}).status_code == 403


def test_behind_a_proxy_that_rewrites_host_the_page_still_works(client):
    """nginx's default `proxy_set_header Host $proxy_host`: the app sees its own
    upstream address while the browser is on the public name. The browser's
    verdict does not depend on Host, so nothing legitimate is refused."""
    headers = {**HTMX, "origin": "https://watch.example.com", "host": "127.0.0.1:8000"}
    assert post(client, "older history", headers).status_code == 200


def test_a_user_typed_request_is_not_another_site(client):
    """`Sec-Fetch-Site: none` — the address bar, a bookmark — no page made it."""
    assert post(client, "older history", {"sec-fetch-site": "none"}).status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_methods_are_refused_from_another_site_and_change_nothing(client, method):
    before = state(client)
    response = client.request(method, f"/wallets/{ui_wallet(client)}",
                              headers={"sec-fetch-site": "cross-site"})
    assert response.status_code in (403, 405)
    assert client.request(method, f"/wallets/{ui_wallet(client)}", headers=HTMX).status_code == 405
    assert state(client) == before


def test_a_refusal_echoes_nothing_it_was_sent(client):
    response = client.post("/wallets", data={"name": "Evil<script>", "address": EVIL},
                           headers={"sec-fetch-site": "cross-site", "origin": "https://evil.example"})
    assert response.status_code == 403
    assert "evil" not in response.text.lower()


# --------------------------------------------------------- reads stay reads


def _every_read(client) -> list[str]:
    activity = client.ctx.db.query_activities(limit=1)[0]
    wallet = ui_wallet(client)
    return [
        "/", "/?q=main&direction=in", f"/?wallet={wallet}",
        "/activity", f"/activity?wallet={wallet}&q=x&dust=show",
        f"/tx/{activity['id']}", "/history/older", "/dust-note",
        "/wallets", "/wallets/form", f"/wallets/form?id={wallet}",
        "/summary", f"/summary?wallet={wallet}", "/holdings", "/status",
        "/api/activity", "/api/accounts", "/api/portfolio", "/api/status", "/healthz",
    ]


def test_no_read_changes_anything(client):
    before = state(client)
    for path in _every_read(client):
        assert client.get(path, headers=HTMX).status_code == 200, path
        assert client.head(path).status_code in (200, 405), path
    assert state(client) == before
    assert client.ctx.db.get_sync_status("ethereum").requested_from_block is None


def test_another_site_cannot_embed_or_fetch_a_page(client, provider):
    """An <img src=…/tx/1> on another site would otherwise spend provider quota
    with the user's remembered credentials, and a <script src=…/api/…> would
    at least be sent."""
    activity = client.ctx.db.query_activities(limit=1)[0]
    asked = []
    original = provider.get_transaction_cost

    async def counting(tx_hash):
        asked.append(tx_hash)
        return await original(tx_hash)

    provider.get_transaction_cost = counting
    for mode, dest in [("no-cors", "image"), ("no-cors", "script"), ("cors", "empty"),
                       ("navigate", "iframe")]:
        response = client.get(f"/tx/{activity['id']}", headers={
            "sec-fetch-site": "cross-site", "sec-fetch-mode": mode, "sec-fetch-dest": dest})
        assert response.status_code == 403, (mode, dest)
    assert asked == []


def test_a_link_from_another_site_still_opens_the_app(client):
    response = client.get("/", headers={"sec-fetch-site": "cross-site",
                                        "sec-fetch-mode": "navigate",
                                        "sec-fetch-dest": "document"})
    assert response.status_code == 200


# -------------------------------------------------------- security headers


def _responses(client, tmp_path, watch_config, provider, prices, sample_transfers):
    """One of every kind of answer the app gives."""
    activity = client.ctx.db.query_activities(limit=1)[0]
    out = {
        "page": client.get("/"),
        "fragment": client.get("/activity", headers=HTMX),
        "inspector": client.get(f"/tx/{activity['id']}", headers=HTMX),
        "json": client.get("/api/status"),
        "healthz": client.get("/healthz"),
        "not found": client.get("/nope"),
        "wrong method": client.get(f"/wallets/{ui_wallet(client)}/remove"),
        "invalid": client.get("/tx/not-a-number"),
        "form error": client.post("/wallets", data={"name": ""}, headers=HTMX),
        "csrf": client.post("/history/older"),
    }

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    client.ctx.db.get_sync_status = boom
    out["server error"] = client.get("/api/status")
    return out


@pytest.fixture
def every_kind(client, tmp_path, watch_config, provider, prices, sample_transfers):
    return _responses(client, tmp_path, watch_config, provider, prices, sample_transfers)


def test_every_answer_carries_the_security_headers(every_kind):
    for kind, response in every_kind.items():
        for name, value in HEADERS.items():
            assert response.headers.get(name) == value, (kind, name, response.status_code)


def test_nothing_private_is_cached(every_kind):
    for kind, response in every_kind.items():
        assert response.headers.get("cache-control") == "no-store", kind


def test_static_files_carry_the_headers_and_stay_cacheable(client):
    response = client.get("/static/app.css")
    assert response.status_code == 200
    for name, value in HEADERS.items():
        assert response.headers.get(name) == value, name
    assert "no-store" not in response.headers.get("cache-control", "")


def test_no_hsts_from_the_app(every_kind):
    """TLS ends at the proxy; whether HTTPS is real is the proxy's to say."""
    assert all("strict-transport-security" not in r.headers for r in every_kind.values())


def test_the_server_error_page_says_nothing(every_kind):
    response = every_kind["server error"]
    assert response.status_code == 500
    assert "boom" not in response.text and "Traceback" not in response.text


# ---------------------------------------------------------- CSP-compatible markup


class _Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            src = attrs.get("src") or ""
            if not src.startswith("/static/"):
                self.problems.append(f"inline or foreign <script> {attrs}")
        if tag == "style":
            self.problems.append("<style> element")
        if tag == "meta" and attrs.get("name"):
            self.meta[attrs["name"]] = attrs.get("content") or ""
        for name, value in attrs.items():
            value = value or ""
            if name.startswith("on") or name.startswith("hx-on"):
                self.problems.append(f"<{tag} {name}=…> handler")
            if name == "style":
                self.problems.append(f"<{tag} style=…>")
            if name in ("href", "src", "action", "hx-get", "hx-post") and \
                    value.strip().lower().startswith("javascript:"):
                self.problems.append(f"<{tag} {name}=javascript:>")
            if name == "hx-trigger" and "[" in value:
                self.problems.append(f"<{tag} hx-trigger> filter needs eval: {value}")
            if name in ("hx-vals", "hx-vars", "hx-headers") and \
                    (value.strip().startswith("js:") or name == "hx-vars"):
                self.problems.append(f"<{tag} {name}> evaluates script")


def _markup(html: str) -> _Markup:
    parser = _Markup()
    parser.feed(html)
    return parser


def test_no_page_or_fragment_needs_inline_script_or_eval(client):
    """What a CSP without 'unsafe-inline' and 'unsafe-eval' would refuse: inline
    <script>, on…= and hx-on handlers, style= attributes, javascript: URLs,
    hx-trigger filters and js: values (htmx runs both through Function)."""
    activity = client.ctx.db.query_activities(limit=1)[0]
    wallet = ui_wallet(client)
    pages = {path: client.get(path, headers=HTMX).text for path in [
        "/", f"/?wallet={wallet}", "/?dust=show", "/activity", f"/tx/{activity['id']}",
        "/history/older", "/dust-note", "/dust-note?dust=show", "/wallets",
        "/wallets/form", f"/wallets/form?id={wallet}", "/summary", "/holdings", "/status",
    ]}
    pages["form error"] = client.post("/wallets", data={"name": ""}, headers=HTMX).text
    for path, html in pages.items():
        assert _markup(html).problems == [], path


def test_htmx_is_told_not_to_evaluate_or_inject(client):
    import json

    config = json.loads(_markup(client.get("/").text).meta["htmx-config"])
    assert config["allowEval"] is False
    assert config["allowScriptTags"] is False
    assert config["includeIndicatorStyles"] is False


# ------------------------------------------------------------------ Host


@pytest.fixture
def pinned(tmp_path, watch_config, provider, prices, sample_transfers):
    app = _client(tmp_path, watch_config, provider, prices, sample_transfers,
                  allowed_hosts=("watch.example.com",))
    with TestClient(app, base_url="http://watch.example.com",
                    raise_server_exceptions=False) as test_client:
        test_client.app_under_test = app
        yield test_client


def from_inside(client) -> TestClient:
    """The same app, asked from 127.0.0.1 — as the container's own healthcheck
    asks it (a proxy on the host reaches the container from the bridge)."""
    return TestClient(client.app_under_test, base_url="http://127.0.0.1:8000",
                      client=("127.0.0.1", 41234), raise_server_exceptions=False)


def test_with_allowed_hosts_set_another_host_is_refused(pinned):
    """DNS rebinding: another name pointed at this address is another origin to
    the browser, which would let that site read an instance with no auth."""
    response = pinned.get("/", headers={"host": "rebind.evil.example"})
    assert response.status_code == 400
    assert "rebind" not in response.text
    for name, value in HEADERS.items():
        assert response.headers.get(name) == value, name
    assert response.headers.get("cache-control") == "no-store"


def test_with_allowed_hosts_set_the_listed_host_is_served(pinned):
    assert pinned.get("/").status_code == 200
    assert pinned.get("/", headers={"host": "WATCH.example.com:443"}).status_code == 200


def test_the_healthcheck_from_inside_the_container_is_served(pinned):
    inside = from_inside(pinned)
    for host in ("127.0.0.1:8000", "localhost", "[::1]:8000"):
        assert inside.get("/healthz", headers={"host": host}).status_code == 200, host


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8000", "localhost", "[::1]"])
def test_a_loopback_host_opens_nothing_but_the_healthcheck(pinned, host):
    """The exception exists for the healthcheck: not for the page, the API or
    a write — not even from 127.0.0.1 itself — and not for /healthz asked
    from outside (a proxy that forwards whatever Host the client sent)."""
    inside = from_inside(pinned)
    for path in ("/", "/api/status", "/activity", "/static/app.css"):
        assert inside.get(path, headers={"host": host}).status_code == 400, path
        assert pinned.get(path, headers={"host": host}).status_code == 400, path
    assert inside.post("/history/older", headers={"host": host, **HTMX}).status_code == 400
    assert pinned.get("/healthz", headers={"host": host}).status_code == 400


def test_behind_a_proxy_that_keeps_host_the_public_name_is_served(pinned):
    """Caddy's default, and nginx with `proxy_set_header Host $host`."""
    headers = {"host": "watch.example.com", "x-forwarded-for": "203.0.113.9",
               "x-forwarded-proto": "https"}
    assert pinned.get("/", headers=headers).status_code == 200
    assert pinned.post("/history/older", headers={
        **HTMX, **headers, "origin": "https://watch.example.com"}).status_code == 200


def test_behind_a_proxy_that_rewrites_host_nothing_is_served(pinned):
    """nginx's default sends its upstream address as Host: refused, plainly,
    and X-Forwarded-Host does not talk the app round — it reads no proxy
    header. The fix is in the proxy (docs/production.md)."""
    response = pinned.get("/", headers={"host": "127.0.0.1:8000",
                                        "x-forwarded-host": "watch.example.com"})
    assert response.status_code == 400
    assert response.text == "Unknown host."


@pytest.mark.parametrize("path", ["/nope", "/tx/x", "/wallets/1/remove", "/"])
def test_a_refused_or_failed_answer_reflects_no_host(pinned, path):
    for host in ("evil.example", "watch.example.com\r\nX-Injected: 1"):
        response = pinned.get(path, headers={"host": host}, follow_redirects=False)
        assert "evil" not in response.text and "Injected" not in response.text
        assert "evil" not in str(response.headers) and "x-injected" not in response.headers
        assert "location" not in response.headers


# ------------------------------------------- Sec-Fetch-Site and Origin together


@pytest.mark.parametrize("site, origin", [
    ("same-origin", "null"),                        # a sandboxed frame
    ("same-origin", "https://evil.example"),        # nothing a browser would send
    ("none", "https://evil.example"),
    (None, "https://evil.example"),                 # an old browser, or a forger
    ("same-origin", "http://localhost:8000"),       # loopback is not a public name
])
def test_with_allowed_hosts_an_origin_that_is_not_listed_is_refused(pinned, site, origin):
    headers = {"hx-request": "true", "origin": origin}
    if site:
        headers["sec-fetch-site"] = site
    before = pinned.get("/api/status").json()["history"]
    assert pinned.post("/history/older", headers=headers).status_code == 403
    assert pinned.get("/api/status").json()["history"] == before


def test_with_allowed_hosts_a_listed_origin_on_any_port_or_scheme_is_taken(pinned):
    for origin in ("https://watch.example.com", "http://WATCH.example.com:8443"):
        response = pinned.post("/history/older", headers={**HTMX, "origin": origin})
        assert response.status_code == 200, origin


def test_a_null_origin_is_refused_even_from_the_same_origin(client):
    assert post(client, "older history", {**HTMX, "origin": "null"}).status_code == 403


def test_without_allowed_hosts_a_proxy_rewriting_host_still_works(client):
    """Unchanged from S8a: with no list, Origin is not compared with Host."""
    headers = {**HTMX, "origin": "https://watch.example.com", "host": "127.0.0.1:8000"}
    assert post(client, "older history", headers).status_code == 200


def test_a_host_header_is_never_reflected(client):
    """By default any Host is served — and none is ever written back: no
    absolute URL, no redirect built from it."""
    for path in ["/", "/activity/", "/wallets/", "/api/status/", "/static"]:
        response = client.get(path, headers={"host": "evil.example"}, follow_redirects=False)
        assert "evil.example" not in response.text, path
        assert "evil.example" not in str(response.headers), path


# ---------------------------------------------- credentials and secrets


SECRET_KEY = "sk-test-0123456789abcdef"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def guarded(tmp_path, watch_config, provider, prices, sample_transfers):
    app = _client(tmp_path, watch_config, provider, prices, sample_transfers,
                  alchemy_api_key=SECRET_KEY, auth_user="rofl", auth_password=PASSWORD)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.ctx = app.state.ctx
        yield test_client


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"authorization": f"Basic {token}"}


def test_credentials_appear_in_no_answer_and_no_log(guarded, caplog):
    good, bad = basic("rofl", PASSWORD), basic("rofl", PASSWORD + "x")
    secrets = [PASSWORD, good["authorization"].split()[1], bad["authorization"].split()[1],
               SECRET_KEY]

    def boom(*args, **kwargs):
        raise RuntimeError(f"failed with {SECRET_KEY} as rofl:{PASSWORD} {good}")

    with caplog.at_level(logging.DEBUG):
        responses = [
            guarded.get("/", headers=good),
            guarded.get("/", headers=bad),
            guarded.get("/api/status", headers=good),
            guarded.post("/history/older", headers={**good, "sec-fetch-site": "cross-site"}),
            guarded.post("/wallets", data={"name": ""}, headers={**good, **HTMX}),
            guarded.get("/nope", headers=good),
            guarded.get("/healthz"),
        ]
        guarded.ctx.db.get_sync_status = boom
        responses += [guarded.get("/api/status", headers=good), guarded.get("/healthz")]

    log_text = caplog.text + "".join(
        str(r.exc_text or "") for r in caplog.records)
    for response in responses:
        dump = response.text + str(response.headers)
        for secret in secrets:
            assert secret not in dump, (response.request.url, response.status_code)
    for secret in secrets:
        assert secret not in log_text


def test_a_401_is_private_and_carries_the_headers(guarded):
    response = guarded.get("/api/status")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers.get("cache-control") == "no-store"
    for name, value in HEADERS.items():
        assert response.headers.get(name) == value, name


def test_authentication_comes_before_the_origin_check(guarded):
    """Without credentials the answer is always 401 — a 403 would tell a
    stranger which requests the app would otherwise have taken."""
    assert guarded.post("/history/older", headers={"sec-fetch-site": "cross-site"}
                        ).status_code == 401
    assert guarded.post("/history/older").status_code == 401


def test_authenticated_writes_follow_the_same_origin_rule(guarded):
    good = basic("rofl", PASSWORD)
    assert guarded.post("/history/older", headers={**good, "sec-fetch-site": "same-site"}
                        ).status_code == 403
    assert guarded.post("/history/older", headers={**good, **HTMX}).status_code == 200


def test_a_stranger_left_by_a_proxy_header_is_not_trusted(client):
    """The app reads no X-Forwarded-* header: nothing it decides depends on one."""
    response = client.post("/history/older", headers={
        "x-forwarded-host": "testserver", "x-forwarded-proto": "https",
        "x-forwarded-for": "127.0.0.1", "forwarded": "for=127.0.0.1;host=testserver",
        "origin": "https://evil.example", "sec-fetch-site": "cross-site"})
    assert response.status_code == 403


def test_a_wallet_added_from_this_page_still_lands(client):
    """The whole of the happy path once more, as the browser does it."""
    response = client.post("/wallets", data={"name": "Fresh", "address": STRANGER,
                                             "chain": "ethereum"}, headers=HTMX)
    assert response.status_code == 200
    assert "wallet-changed" in response.headers["hx-trigger"]
    assert re.search("Fresh", client.get("/wallets").text)
