# Browser smoke tests

`tests_ui/` drives the real page in Chromium (Playwright) against a local
stand. They are separate from the main suite in `tests/`: `pytest` on its own,
CI's `pytest` job and `scripts/deploy.sh` do not run them.

## Running them

```bash
.venv/bin/pip install -r requirements-dev.txt -r requirements-ui.txt
.venv/bin/python -m playwright install --only-shell chromium   # once, ~125 MB

.venv/bin/python -m pytest tests_ui -rxX     # about 30 s
.venv/bin/python -m pytest tests_ui -rxX -k inspector
```

Without Playwright installed the whole directory is skipped with that hint,
rather than failing.

### On a slow server

```bash
BLOCKTAIL_UI_LATENCY_MS=400 .venv/bin/python -m pytest tests_ui   # every htmx answer 400 ms late
BLOCKTAIL_UI_CPU_SLOWDOWN=4 .venv/bin/python -m pytest tests_ui   # the page on a quarter of the CPU
```

The latency is the stand's: the server holds each htmx request before
answering it, each on its own, so requests overlap and finish in the order a
busy server would finish them. The suite passes at 0, 100 and 400 ms.

A test waits for what it is about to read, never for a fixed time:
`feed_answers(page)` (conftest) until the feed shows the answer to the
question the filter form asks now and htmx has nothing in flight — whatever
followed the answer (the hidden-transfers note, the sidebar, the summary)
included; `htmx_idle(page)`; or the element itself (`locator.wait_for()`).
`wait_for_load_state("networkidle")` is no such wait: it is a load state,
reached once when the page loaded, and returns at once afterwards.

## What they run against

`tests_ui/stand.py` starts the real application — the real indexer, the real
migrations, a temporary SQLite database — on a free local port, fed by the
test suite's `FakeProvider` and `FakePrices`:

* six wallets (five mine, one watched), one of them with a very long name;
* about a hundred transfers over a month, ETH and USDC, a four-transfer swap,
  a transfer between two of my wallets and one to the watched wallet, a
  labelled counterparty ("Binance hot");
* the extremes: a scam token (NMT) with a 34-digit amount and an absurd quote,
  a forged ticker (`ꓴꓢꓓ⊤`), a 1-wei transfer;
* one transfer older than the 30-day backfill window, for "Load more", and a
  wallet that a test adds and then removes.

The data is the same every run; timestamps are relative to when the stand
starts, so "Today" always has rows. No key, no URL and no request reaches
anything outside the stand: each page aborts any request that is not for the
stand and the test fails if one was attempted.

## What they check

| Area | Viewports |
| --- | --- |
| Dashboard renders: feed, status, day headings, total | 1440, 768, 390, 320 |
| No horizontal overflow | 1440, 768, 390, 320 |
| Switching wallets and scopes (sidebar; the scope sheet on a phone) | 1440, 390 |
| Search, direction and asset filters | 1440 |
| Transaction inspector opens and closes | 1440, 390 |
| Mobile holdings sheet | 390 |
| "Load N more days": queued → running → completed, rows appear | 1440 |
| Removing a wallet updates sidebar, feed and scope | 1440 |
| No axe-core violations | 1440, 390 (smoke); 1440, 768, 390, 320 × dark, light (`test_accessibility.py`) |
| Main controls reachable with Tab | 1440 |
| Keyboard and focus (`test_accessibility.py`, below) | 1440, 768, 390, 320 |
| Search, filters, wallet events, skip link (`test_interaction.py`, below) | 1440, 768, 390, 320 |
| Connection status (`test_connection.py`, below) | 1440, 390 |
| Phones and money (`test_mobile_financial.py`, below) | 1440, 1024, 768, 430, 390, 360, 320 |
| No JavaScript or console errors, no CSP violation, no request leaves the stand | every test |
| Served under the CSP; inline script refused; every former `onclick` control works; basic auth in a browser (`test_security.py`) | 1440, 390 |

The history test holds the stand's deep fetch behind a gate, so "running" is
observable, then opens it.

### Connection status

`test_connection.py` takes the stand's server down while the page stays open
(`Stand.take_down()`) and brings it back on the same port
(`Stand.bring_back()`), so the browser sees exactly what a crashed or
restarting server looks like. It checks that the page says the connection is
lost within one poll, keeps its data, keeps counting the age, and returns to a
current status without a reload; that a provider failure, a balance warning
and a history request are each shown as what they are and not as a lost
connection; and that the status, replaced every poll, is watched by one
handler and one timer however many polls go by.

While the server is down the browser logs the failed requests. Those tests
declare exactly those messages (`session.expect_console_errors(...)`:
`ERR_CONNECTION_REFUSED`, "Failed to load resource", and htmx's
`htmx:sendError` / `htmx:afterRequest`); any other console error still fails
the test. A connection test takes 5–15 s, most of it waiting for polls.

### Phones and money

`test_mobile_financial.py` holds the page to one rule: a number is never cut.
It checks, at every width from 1440 down to 320 (430 and 360 included), that
no page — the dashboard, everything shown, the scam token's wallet, the
long-named wallet — is wider than the screen; that no amount, value or total
is clipped or ends in an ellipsis, with the inspector open or not; that a very
large amount shown compactly in a row ("1×10³⁰") is read exactly in the
inspector, one tap away, and is in the row's text for a screen reader; that
the holdings sheet lists the holdings, scrolls, closes, and filters the feed
by the one chosen; that the inspector's close button is on screen and works
on every phone; that unknown decimals read "raw" and carry no USD; that an
excluded asset or a forged ticker shows no made-up value; and that a drawer
left open across a resize to desktop and back leaves nothing broken.

### Keyboard, focus and accessibility

`test_accessibility.py` holds the page to the keyboard model below, and to
zero axe-core violations.

* **The feed is one Tab stop.** Each row's time is a real `<button
  class="row__open">`, so Enter and Space open the inspector without
  reimplementing them. Only the current row is in the Tab order (roving
  tabindex): its button, its own links and its fold-out. The arrows,
  PageUp/PageDown, Home and End choose the row; Tab walks that one row and
  leaves the feed. Rows that arrive later — a refresh, a filter, a page
  appended by scrolling — are brought into line by a `MutationObserver`, and a
  swap that replaces the focused row puts focus back on the same activity.
* **The inspector** gets focus on its heading when it opens and gives it back
  to the row it was opened from when it closes. On a phone it is a modal
  (`role="dialog"`, `aria-modal`, `aria-labelledby`): everything behind it
  is `inert`, Tab is trapped inside, Escape closes it. On a desktop it is a
  side panel, not a modal, and the page stays usable beside it.
* **The scope sheet and the holdings/wallet dialog** follow the same modal
  rules. The dialog is a native `<dialog>`; Chromium lets Tab leave a modal
  dialog for the browser chrome, so its Tab is trapped too. Opening one drawer
  while another is open, and resizing between phone and desktop with one
  open, leave exactly one modal, or none.
* **Exact amounts** are not behind hover: on a desktop "All holdings" opens
  the same sheet a phone uses, where each figure is printed in full, as
  selectable text, wrapping only between digit groups.
* **Labels in words, not colour:** an asset whose quote the total refuses
  reads "not valued", a ticker that imitates a known token from another
  contract reads "unverified" — in the feed, the inspector and the holdings
  sheet (the summary strip says "not valued" too). Neither claims fraud.
* **A refused wallet form** (U-11) marks the field at fault
  (`aria-invalid`, `aria-describedby` pointing at the message), moves focus to
  it, and announces the message (`role="alert"`).
* **Colour schemes:** the page follows `prefers-color-scheme`; both the dark
  and the light palette meet WCAG AA contrast, and axe runs in both.

### Filters, search and the page's events

`test_interaction.py` holds the page to one way of updating itself.

* **One request path.** Every change of what the feed shows — the filter
  controls, a wallet or group in the sidebar, an asset in the summary or the
  holdings sheet, a counterparty in a row, a wallet change — sets the filter
  form's fields and lets the form ask for `/activity`. Links say what they
  choose (`data-pick="wallet:3"`, `scope:mine`, `asset:12`, `address:0x…`)
  and keep a real `href` for a new tab. After the feed is swapped, the address
  bar is updated and `filtersApplied` (the hidden-transfers note) and
  `refreshWallets` (sidebar, summary, status bar) follow — once each.
* **Search** answers `input`, debounced 300 ms: typing, Ctrl+V, the context
  menu's Paste and the native clear all search; typing a word sends one
  request.
* **No stale answers.** The form queues its requests (`hx-sync="this:queue
  last"` — nothing is aborted, so htmx logs nothing), and a feed response is
  swapped in only if the filters in its own URL are still the form's. The
  test holds the first answer back until after the second, and checks the
  rows answer the second.
* **Wallet changes are one event.** The server sends `wallet-changed`
  `{action: added | updated | removed, id}`; its one handler closes the
  dialog, drops a removed wallet from the scope, then updates the feed and,
  after it, the rest. The test checks that nothing asks the server about the
  removed wallet and that each fragment is asked for once.
* **A link to a removed (or unknown) wallet** says so in the feed, with
  "Show all activity"; the summary panel falls back to my wallets; the
  narrow selector reads "Removed wallet", not "All activity". Back and
  forward through such a link show the same.
* **Pages loaded by scrolling** survive the status poll and the 15-second
  refresh (U-12): the test waits through both.
* **Skip to activity** is the first Tab stop on every width, visible only
  while focused, and Enter lands on the current row of the feed — also after
  the feed has been replaced.

## Known defects

These are open findings from [the audit](audit/product-audit.md). Each is a
test marked `xfail(strict=True)` with its id: it is expected to fail today,
and the day the defect is fixed it passes unexpectedly and **fails the run**,
so the marker is removed together with the defect.

None today, in the browser tests or in the main suite.

Fixed and since removed from this list, by the strict markers failing as
designed: U-03 (overflow at 390 and 320, and the inspector's close button
pushed off screen by it) and U-02 (the empty holdings sheet), both in S5;
U-06 (feed rows not reachable by keyboard), in S6; U-10 (a paste did not
search), in S7; U-09 (built-in names not searchable) and `%`/`_` read as
LIKE wildcards, in S7.1 — `tests/test_search.py` covers search, and
`test_interaction.py` checks it in the page after filtering and within a
wallet.

Accessibility (U-07) was a baseline of known axe-core rules until S6, which
fixed them (`color-contrast`, `region`, `empty-table-header`, `select-name`)
and removed the baseline: any axe violation now fails the run, and the
failure message lists every rule with its impact and node count.

`axe-core` 4.10.2 is vendored in `tests_ui/vendor/` (MPL-2.0, licence beside
it), byte-identical to the npm package, so the check works offline.

## When a test fails

Failures name the viewport and the scenario, e.g.
`[mobile-390] the close button is on screen`, and the test id carries the
viewport (`test_the_inspector_opens_and_closes[mobile-390]`).

For each failed test a directory is written under `$BLOCKTAIL_UI_ARTIFACTS`
(default `tests_ui/artifacts/`, gitignored; in CI it is uploaded as the
`ui-smoke-artifacts` artifact of the run, kept seven days):

| File | What it is |
| --- | --- |
| `page.png` | full-page screenshot at the moment of failure |
| `trace.zip` | the Playwright trace: every action, DOM snapshots, network, console |
| `console.txt` | JavaScript and console errors the page produced |
| `server.log` | the stand's application log (migrations, sync cycles, warnings) |

Open a trace with `python -m playwright show-trace trace.zip` (or drop it on
<https://trace.playwright.dev>, which runs locally in the browser).

## In CI

The `ui-smoke` job in `.github/workflows/tests.yml` installs the app's
requirements, `requirements-ui.txt` and Chromium, runs `pytest tests_ui` with
a 15-minute limit, and uploads the artifacts if it fails. It has read-only
repository permissions and no secrets. The `lint` job checks `tests_ui` too;
the main `pytest` job does not run it.
