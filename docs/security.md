# HTTP security

What the web layer accepts, from whom, and what every answer carries. The code
is `app/security.py` (origin checks, headers, Host) and `app/auth.py` (basic
auth); the tests are `tests/test_http_security.py` and
`tests_ui/test_security.py`.

## The surface

### Writes

| Route | Does | Called by |
| --- | --- | --- |
| `POST /wallets` | adds a wallet (`source = 'ui'`) | the add form, through htmx |
| `POST /wallets/{id}` | renames it, or moves it between *mine* and *watching* | the edit form; the sidebar menu (`hx-vals`) |
| `POST /wallets/{id}/remove` | stops watching a wallet added here | the sidebar menu (`hx-confirm`) |
| `POST /history/older` | records a request for older history and wakes the indexer | the end-of-feed form, through htmx |

There is nothing else: no settings, no PUT, PATCH or DELETE. Any other method
on these paths is a 405.

### Reads

| Route | Answer | Side effect |
| --- | --- | --- |
| `GET /` | the page | none |
| `GET /activity`, `/dust-note`, `/wallets`, `/wallets/form`, `/summary`, `/holdings`, `/status`, `/history/older` | HTML fragments for htmx | none |
| `GET /tx/{id}` | the inspector | **asks the provider** for the fee (one read, uncached; costs quota) |
| `GET /api/activity`, `/api/accounts`, `/api/portfolio`, `/api/status` | JSON | none |
| `GET /healthz` | liveness; open without auth, and then says only `ok` | none |
| `GET /static/*` | `app.css`, `boot.js`, `app.js`, `htmx.min.js` | none |

Every read leaves the database as it was: `test_no_read_changes_anything`
dumps it before and after calling each route. `GET /history/older` only
reports the progress of a request; asking for one is the POST.

The page polls `/status` (5 s), `/wallets` and `/summary` (30 s), the feed
(15 s), and `/history/older` (3 s, while a request runs).

### State the browser keeps

No cookies: the app sets none, and nothing reads one. `localStorage` holds two
display preferences (holdings folded, hidden tokens shown).

### Script and style

All from this origin: `app.css`, `htmx.min.js` 2.0.4, `boot.js` (in `<head>`,
before first paint) and `app.js` (end of the page). No CDN, no font, no
third-party request. The favicon is an inline SVG (`data:`). External links
(Etherscan) are plain `<a target="_blank" rel="noopener noreferrer">`.

Until S8a the page had two inline `<script>` blocks, 17 `onclick=`, an
`onsubmit=`, three `hx-on::after-request` and two `hx-trigger` filters —
the last two are evaluated by htmx through `Function`, which a CSP without
`'unsafe-eval'` refuses. They are now code in `app.js`: a control names its
action (`data-action="toggle-dust"`), one delegated listener runs it, and the
former filters are events (`open-row`, and a `change` listener on the form).

## Writes come from this page only (CSRF)

The app's one credential is HTTP basic auth — its own or the proxy's — and a
browser attaches it by itself to every request for this origin, including one
another site makes it send. Basic auth has no `SameSite`. So the browser's own
account of where a request came from decides (`write_refusal`):

1. `Sec-Fetch-Site` present → only `same-origin` (this page) or `none` (the
   user, from the address bar) is taken. `same-site` is another site — any
   subdomain of the same domain (D-11) — and `cross-site` plainly so.
2. No fetch metadata at all (a browser from before 2023, curl, a script) →
   only with `HX-Request: true`. Every htmx request sends it. A page on
   another origin cannot add a custom header without a CORS preflight, and
   this app answers no preflight.
3. Neither → 403. An old browser posting a form from another site looks
   exactly like this, and so does a request with only `Origin` or `Referer`.

`Sec-Fetch-Site` is a forbidden header: no page can set or remove it. Neither
check reads `Host`, `Origin` or `X-Forwarded-*`, so a proxy that rewrites Host
(nginx's default `proxy_set_header Host $proxy_host`) cannot make the page's
own requests look foreign. Nothing is stored, issued or signed: there is no
token to leak into a URL, a page, a log or an error.

A script that writes (curl, a cron job) sends `-H 'HX-Request: true'` along
with its credentials.

**Reads** from another site are refused unless they are a top-level navigation
(`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`): a link to the app
from elsewhere opens it, but an `<img src=…/tx/1>` elsewhere — which would
spend provider quota with the user's remembered credentials — and a
`<script src=…/api/…>` or a frame are refused.

The check sits inside authentication: without credentials the answer is 401
whatever the origin.

## Headers on every answer

Set by the outermost middleware, so pages, fragments, JSON, static files,
401, 403, 404, 405, 422 and 500 all carry them.

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
    img-src 'self' data:; connect-src 'self'; form-action 'self';
    base-uri 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: same-origin
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Cache-Control: no-store          (everything except /static/*)
```

* **CSP.** No `'unsafe-inline'`, no `'unsafe-eval'`, no nonce. htmx is told
  the same before it loads (`<meta name="htmx-config">`): `allowEval: false`,
  `allowScriptTags: false`, `includeIndicatorStyles: false` — otherwise it
  would try to inject a `<style>` the policy refuses. Setting a CSS custom
  property from script (`style.setProperty`) is not inline style and is
  allowed. `test_no_page_or_fragment_needs_inline_script_or_eval` parses every
  page and fragment for what the policy would refuse; every browser test fails
  on any `securitypolicyviolation`.
* **Framing.** `frame-ancestors 'none'` and `X-Frame-Options: DENY`: a framed
  page would make clickjacked clicks same-origin (D-10).
* **Referrer.** `same-origin`, not `no-referrer`: a link to Etherscan leaves
  without the address in `?q=`, and the page's own requests keep a real
  `Origin`.
* **Cache.** Nothing private is stored by the browser or a shared cache.
  Static files keep their cache-busting `?v=` and normal caching.
* **No HSTS.** TLS ends at the proxy; whether HTTPS is real, and for which
  names, is the proxy's to declare. Set it there.
* **No `Permissions-Policy`.** The page requests no permission and embeds
  nothing; clipboard writes are a user gesture.

An unhandled error answers `Internal Server Error` and nothing else; the
traceback goes to the log, scrubbed of registered secrets.

## Basic auth

`AUTH_USER` and `AUTH_PASSWORD` both set → every route but `/healthz` needs
them (`app/auth.py`, unchanged in S8a). Wrong or missing → 401 with
`WWW-Authenticate: Basic`, the headers above and `no-store`.

* The password and the `Authorization` value itself (base64 of
  `user:password`) are registered secrets: any log line or traceback that
  contains either is masked. Nothing logs request headers; uvicorn's access
  log has the request line only.
* No answer — page, error, refusal — contains the credentials
  (`test_credentials_appear_in_no_answer_and_no_log`).
* Basic auth is sent with every request, reversibly encoded: over plain HTTP
  it is in the clear. Behind TLS or on a private network only.

## Host and the proxy

* **Host.** By default any Host is served, and none is ever written back: the
  app builds no absolute URL and no redirect from it (`redirect_slashes` is
  off — that redirect was the one place Host was reflected). Set
  `ALLOWED_HOSTS=watch.example.com` and any other name is answered 400 — the
  defence against DNS rebinding for an instance without auth. `localhost`,
  `127.0.0.1` and `[::1]` are always accepted: the container healthcheck asks
  by address, and a rebinding site's requests carry its own name.
  `*.example.com` matches subdomains.
* **Proxy headers.** The app reads no `X-Forwarded-*` or `Forwarded` header,
  and nothing it decides depends on scheme or client address. uvicorn's own
  default (`--proxy-headers`, trusted from `FORWARDED_ALLOW_IPS`, by default
  `127.0.0.1` only) affects at most the client address in its access log.
* **HTTP and HTTPS.** The app behaves the same on both. The clipboard API
  needs a secure context, so over plain HTTP "Copy address" shows the address
  to copy by hand.

## Open decisions

* `ALLOWED_HOSTS` is empty by default, so an existing deployment keeps
  working. An instance reachable without auth should set it.
* HSTS belongs in the proxy config once its HTTPS is confirmed.
* A browser without fetch metadata (Safari before 16.4, Chrome before 76,
  Firefox before 90) cannot post a plain form without script; with script,
  htmx sends `HX-Request` and everything works.
