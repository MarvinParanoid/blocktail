"""The status bar tells the truth about the connection (audit U-01).

Before: when the server stopped answering, the dot stayed green and the bar
kept saying "2s ago" — the age was text the server rendered, and a page that
no longer reached the server simply never changed.

What these hold the page to:

* while the server answers, the status is current, and its age counts up in
  the browser between polls;
* when a poll fails, the page says the connection is lost, keeps the data it
  has, and keeps counting the age of what it shows;
* when the server answers again, the status is current again, without a
  reload;
* a provider failure, a balance warning and a history request are each
  told apart from a lost connection, and from each other.
"""

from __future__ import annotations

import re
import time

from conftest import DESKTOP, MOBILE, SCRIPT, viewports

from tests.conftest import ProviderDown

# What the browser logs when requests to a stopped server fail.
OUTAGE_NOISE = (
    r"ERR_CONNECTION_REFUSED",
    r"Failed to load resource",
    r"^console\.error: htmx:sendError$",
    r"^console\.error: htmx:afterRequest$",
)
POLL = 5  # seconds between status polls (hx-trigger "every 5s")


def html_classes(page) -> str:
    return page.locator("html").get_attribute("class") or ""


def age_seconds(page, viewport) -> int:
    """The age the bar shows, in seconds. On a phone it is the status's
    data-age (rendered by CSS); on a wide screen the visible age span."""
    if viewport.mobile:
        text = page.locator(".status").get_attribute("data-age") or ""
    else:
        text = page.locator(".status__age").inner_text()
    match = re.match(r"(\d+)([smhd]) ago", text.strip())
    assert match, f"no age in {text!r}"
    value, unit = int(match.group(1)), match.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def wait_for(page, js: str, timeout: float = POLL + 4):
    page.wait_for_function(js, timeout=timeout * 1000)


LOST = "() => document.documentElement.classList.contains('conn-lost')"
BACK = "() => !document.documentElement.classList.contains('conn-lost')"


@viewports(DESKTOP, MOBILE)
def test_a_reachable_server_shows_a_current_status(fresh_session, viewport):
    page = fresh_session.open("/")
    assert page.locator(".status--ok").count() == 1, fresh_session.where("synced")
    assert "conn-lost" not in html_classes(page)
    assert page.locator("#connection").is_hidden(), fresh_session.where("no connection notice")
    first = age_seconds(page, viewport)
    page.wait_for_timeout(2300)
    assert age_seconds(page, viewport) >= first + 2, fresh_session.where("the age counts up")


@viewports(DESKTOP, MOBILE)
def test_a_lost_server_is_shown_and_the_data_is_kept(fresh_session, viewport):
    session = fresh_session
    page = session.open("/")
    rows_before = page.locator("#feed-body tr[data-activity]").count()
    total_before = page.locator(".figure__main").first.inner_text()
    ok_colour = page.evaluate(
        "() => getComputedStyle(document.querySelector('.status__dot')).backgroundColor"
    )

    session.expect_console_errors(*OUTAGE_NOISE)
    session.stand.take_down()
    try:
        wait_for(page, LOST)
        notice = page.locator("#connection")
        assert notice.is_visible(), session.where("the notice is shown")
        assert "connection lost" in notice.inner_text().lower(), session.where(notice.inner_text())
        dot = page.evaluate(
            "() => getComputedStyle(document.querySelector('.status__dot')).backgroundColor"
        )
        assert dot != ok_colour, session.where("the dot no longer says 'current'")

        first = age_seconds(page, viewport)
        page.wait_for_timeout(3200)
        assert age_seconds(page, viewport) >= first + 3, session.where("the age keeps growing")

        assert page.locator("#feed-body tr[data-activity]").count() == rows_before, \
            session.where("the feed is kept")
        assert page.locator(".figure__main").first.inner_text() == total_before, \
            session.where("the total is kept, not zeroed")
    finally:
        session.stand.bring_back()


@viewports(DESKTOP, MOBILE)
def test_the_status_recovers_without_a_reload(fresh_session, viewport):
    session = fresh_session
    page = session.open("/")
    page.evaluate("() => { window.__sameDocument = true }")

    session.expect_console_errors(*OUTAGE_NOISE)
    session.stand.take_down()
    try:
        wait_for(page, LOST)
    finally:
        session.stand.bring_back()
    wait_for(page, BACK, timeout=POLL + 6)

    assert page.evaluate("() => window.__sameDocument === true"), session.where("no reload")
    assert page.locator(".status--ok").count() == 1, session.where("current again")
    notice = page.locator("#connection")
    assert notice.is_hidden() or "reconnected" in notice.inner_text().lower()


@viewports(DESKTOP)
def test_a_provider_failure_is_not_a_lost_connection(fresh_session, viewport):
    session = fresh_session
    page = session.open("/")
    session.stand.provider.fail_with = ProviderDown("alchemy: HTTP 503 from the provider")
    page.request.post(session.stand.url + "/__stand__/sync", headers=SCRIPT)
    wait_for(page, "() => !!document.querySelector('.status--error')", timeout=POLL + 6)

    detail = page.locator(".status__detail").inner_text()
    assert "HTTP 503" in detail, session.where(f"the provider's error is named: {detail!r}")
    assert "conn-lost" not in html_classes(page), session.where("the server is reachable")
    assert page.locator("#connection").is_hidden()


@viewports(DESKTOP, MOBILE)
def test_a_balance_warning_is_not_shown_as_fully_healthy(fresh_session, viewport):
    session = fresh_session
    page = session.open("/")
    stand = session.stand
    stand.provider.fail_balances_with = ProviderDown("HTTP 503 from the balance API")
    stand.app.state.ctx.indexer._balances_at = None   # the next cycle reads every wallet
    page.request.post(stand.url + "/__stand__/sync", headers=SCRIPT)
    wait_for(page, "() => !!document.querySelector('.status__detail--balances')",
             timeout=POLL + 6)

    status = page.locator(".status")
    assert "status--partial" in (status.get_attribute("class") or ""), \
        session.where("indexing is current, balances are not: not plain 'ok'")
    assert "balances not refreshed" in page.locator(".status__detail--balances").inner_text()
    assert "conn-lost" not in html_classes(page)


@viewports(DESKTOP)
def test_a_history_request_is_shown_in_the_status(fresh_session, viewport):
    session = fresh_session
    stand = session.stand
    page = session.open("/")
    stand.provider.gate.clear()
    try:
        page.request.post(stand.url + "/history/older", headers=SCRIPT)
        wait_for(page, "() => /older history/i.test("
                       "document.querySelector('.status__history')?.innerText || '')")
        note = page.locator(".status__history").inner_text().lower()
        assert "older history" in note and ("queued" in note or "indexing" in note)
    finally:
        stand.provider.gate.set()
    wait_for(page, "() => !document.querySelector('.status__history')", timeout=POLL + 8)


@viewports(DESKTOP)
def test_swaps_do_not_add_handlers_or_timers(fresh_session, viewport):
    """The status is replaced every poll. Whatever watches it must be wired
    once, not once per swap: one handled response per request, one tick per
    second."""
    session = fresh_session
    page = session.open("/")
    responses = []
    page.on("response", lambda r: responses.append(r) if "/status" in r.url else None)
    start = page.evaluate("() => ({...window.blocktail.connection.stats})")
    started = time.monotonic()
    page.wait_for_timeout(int(3.2 * POLL * 1000))
    stats = page.evaluate("() => ({...window.blocktail.connection.stats})")
    elapsed = time.monotonic() - started

    handled = stats["handled"] - start["handled"]
    assert handled == len(responses) >= 3, session.where(
        f"{handled} handled for {len(responses)} status responses"
    )
    ticks = stats["ticks"] - start["ticks"]
    assert abs(ticks - elapsed) <= 2, session.where(f"{ticks} ticks in {elapsed:.1f}s")
