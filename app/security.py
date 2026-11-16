"""HTTP hardening: where a request may come from, and what every answer carries.

The one credential this app knows is HTTP basic auth (app/auth.py, or the
proxy in front), and a browser attaches it by itself to any request for this
origin — including one that another site makes it send. Cookies have SameSite
for that; basic auth has nothing. So the browser's own account of where a
request came from decides:

* **A write** (anything but GET/HEAD/OPTIONS) is taken only when
  `Sec-Fetch-Site` is `same-origin` — this page sent it — or `none` — the user
  did, from the address bar. `same-site` is another site: any subdomain of the
  same domain (D-11). With no fetch metadata at all (an old browser, curl) the
  request must carry `HX-Request: true`, which every htmx request does: a page
  on another origin cannot add a custom header without a CORS preflight, and
  this app answers no preflight. Neither check reads Host, so a proxy that
  rewrites Host cannot make the page's own requests look foreign. An Origin,
  when sent, is never `null`, and — once ALLOWED_HOSTS lists this instance's
  names — must be one of them (S8b).

* **A read** from another site is taken only as a top-level navigation — a link
  to the app. An `<img src=…/tx/1>` elsewhere would otherwise spend provider
  quota with the user's remembered credentials, and a `<script src=…>` would at
  least be sent.

Nothing here keeps a session, sets a cookie or issues a token: there is no
secret to leak into a URL, a page or a log.

Every response — page, fragment, JSON, static file, error, refusal — carries
the same headers, and everything but the static files is `no-store`. The
policy allows script and style from this origin only; see docs/security.md.
"""

from __future__ import annotations

import logging

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("blocktail.security")

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",        # the favicon is an inline SVG
    "connect-src 'self'",          # htmx
    "form-action 'self'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    # For browsers that predate frame-ancestors.
    "X-Frame-Options": "DENY",
    # Not no-referrer: the page's own requests keep their Origin, and a link to
    # Etherscan still leaves without the address it was searched for.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
STATIC_PREFIX = "/static/"
# The container healthcheck asks for /healthz by address, from inside the
# container. That one request, and nothing else, passes a host list by being
# on loopback: not the page, not the API, not /healthz asked from outside.
HEALTHCHECK_PATH = "/healthz"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})


def write_refusal(headers: Headers, allowed_hosts: tuple[str, ...] = ()) -> str | None:
    """Why a state-changing request is refused, or None to let it through.

    With ALLOWED_HOSTS set, an Origin, when sent, must also name one of them:
    a browser on this page always sends its public name, whatever Host the
    proxy passes on, so a mismatch is something no browser on this page sends.
    Without the list Origin is not compared — Host may be the proxy's — but a
    `null` Origin (a sandboxed frame, a data: URL) is never this page."""
    origin = headers.get("origin")
    if origin is not None:
        if origin.strip().lower() == "null":
            return "Origin: null"
        if allowed_hosts and "*" not in allowed_hosts and \
                not _listed(_host_name(origin.split("://", 1)[-1]), allowed_hosts):
            return "Origin not in ALLOWED_HOSTS"
    site = headers.get("sec-fetch-site")
    if site is not None:
        return None if site in ("same-origin", "none") else f"Sec-Fetch-Site: {site}"
    if headers.get("hx-request") == "true":
        return None
    return "no Sec-Fetch-Site and no HX-Request"


def read_refusal(headers: Headers) -> str | None:
    """Why a read is refused: only another site embedding or fetching it."""
    site = headers.get("sec-fetch-site")
    if site not in ("cross-site", "same-site"):
        return None
    if headers.get("sec-fetch-mode") == "navigate" and headers.get("sec-fetch-dest") == "document":
        return None
    return f"Sec-Fetch-Site: {site}, mode {headers.get('sec-fetch-mode')}"


def _host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):                       # [v6]:port
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _listed(name: str, allowed: tuple[str, ...]) -> bool:
    for entry in allowed:
        entry = entry.strip().lower()
        if entry.startswith("*.") and name.endswith(entry[1:]):
            return True
        if name == entry:
            return True
    return False


def host_allowed(host: str, allowed: tuple[str, ...], *, path: str = "",
                 client: str | None = None) -> bool:
    if not allowed or "*" in allowed:
        return True
    name = _host_name(host)
    if _listed(name, allowed):
        return True
    return (name in LOOPBACK_HOSTS and path == HEALTHCHECK_PATH
            and client in LOOPBACK_CLIENTS)


class OriginCheckMiddleware:
    """Refuse a write that another site made, and a read another site embeds.

    Inside authentication: a request without credentials is answered 401
    whatever its origin, so a stranger learns nothing about what would have
    been taken."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: tuple[str, ...] = ()) -> None:
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        method = scope["method"].upper()
        reason = (read_refusal(headers) if method in SAFE_METHODS
                  else write_refusal(headers, self.allowed_hosts))
        if reason is None:
            await self.app(scope, receive, send)
            return
        # The path is ours and the reason is a header value from a fixed set;
        # nothing the sender chose beyond that is written down.
        log.warning("refused %s %s from another origin (%s)", method, scope["path"], reason)
        response = PlainTextResponse("Refused: this request did not come from this page.",
                                     status_code=403)
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """The outermost layer: every answer leaves through it, refusals and
    errors included, and none leaves without the headers.

    Also the Host check, when ALLOWED_HOSTS names the hosts this instance is
    served as — the defence against DNS rebinding for an instance without
    authentication."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: tuple[str, ...] = ()) -> None:
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        private = not scope["path"].startswith(STATIC_PREFIX)
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if private or message.get("status", 200) >= 400:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        host = Headers(scope=scope).get("host", "")
        client = (scope.get("client") or (None,))[0]
        if not host_allowed(host, self.allowed_hosts, path=scope["path"], client=client):
            log.warning("refused %s for a host not in ALLOWED_HOSTS", scope["path"])
            response = PlainTextResponse("Unknown host.", status_code=400)
            await response(scope, receive, send_with_headers)
            return

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if started:
                raise
            # Answered here so the 500 carries the headers too; re-raised so
            # the server still logs the traceback (scrubbed: app/redact.py).
            response = PlainTextResponse("Internal Server Error", status_code=500)
            await response(scope, receive, send_with_headers)
            raise
