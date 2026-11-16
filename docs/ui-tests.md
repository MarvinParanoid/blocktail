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
| axe-core matches the known baseline | 1440, 390 |
| Main controls reachable with Tab | 1440 |
| Connection status (`test_connection.py`, below) | 1440, 390 |
| No JavaScript or console errors, no request leaves the stand | every test |

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

## Known defects

These are open findings from [the audit](audit/product-audit.md). Each is a
test marked `xfail(strict=True)` with its id: it is expected to fail today,
and the day the defect is fixed it passes unexpectedly and **fails the run**,
so the marker is removed together with the defect.

| Audit id | Test | What fails |
| --- | --- | --- |
| U-03 | `test_the_dashboard_has_no_horizontal_overflow[mobile-390]`, `[small-320]` | the NMT row's amount makes the page 51 px (390) and 120 px (320) wider than the screen |
| U-03 | `test_the_inspector_close_button_is_on_screen_on_the_dashboard[mobile-390]` | the same overflow widens the layout, and the inspector's × lands past the right edge |
| U-02 | `test_the_holdings_sheet_shows_its_assets[mobile-390]` | the sheet opens, but every item is hidden by CSS |
| U-06 | `test_the_inspector_opens_from_the_keyboard[desktop-1440]` | feed rows do not take focus |
| U-10 | `test_pasting_into_search_filters_the_feed[desktop-1440]` | an input with no keyup (a paste) does not search |

U-03 is scoped by a passing test next to it: the same page without the NMT
row fits a 390 and a 320 screen, and the inspector there closes normally.

Accessibility (U-07) is a baseline rather than an xfail. `KNOWN_AXE` in
`test_smoke.py` lists the axe-core rules reported today — `color-contrast`,
`region` and `empty-table-header` on desktop; `color-contrast`, `region` and
`select-name` on a phone. The comparison is exact: a new rule fails the run,
and so does a rule that stopped being reported, so the list has to shrink as
fixes land. The failure message lists every rule with its impact and node
count.

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
