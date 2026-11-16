"""Browser smoke tests: the real page, in Chromium, against the local stand.

Known defects from docs/audit/product-audit.md that are not fixed yet are
marked xfail(strict=True) with their audit id. Strict: the day one is fixed,
its test passes unexpectedly and fails the run, so the marker has to be
removed together with the defect — nothing stays silently excused.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
from conftest import DESKTOP, MOBILE, SMALL, TABLET, run_axe, viewports

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stand import REMOVABLE, SWAP_SEED

from tests.conftest import tx_hash

ROWS = "#feed-body tr[data-activity]"


def known(audit_id: str, what: str):
    return pytest.mark.xfail(strict=True, reason=f"known defect {audit_id}: {what}")


def rows(page):
    return page.locator(ROWS)


def overflow(page, viewport) -> int:
    """How far the document is wider than the viewport, in CSS px. With
    mobile emulation the layout viewport grows to fit the content, so the
    test is against the device width, not innerWidth."""
    width = page.evaluate("() => document.documentElement.scrollWidth")
    return width - viewport.width


def wait_for_feed(page):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(150)


# ------------------------------------------------------- pages and layout


@viewports(DESKTOP, TABLET, MOBILE, SMALL)
def test_the_dashboard_renders(session, viewport):
    page = session.open("/")
    assert rows(page).count() >= 20, session.where("feed: first page of events")
    assert page.locator(".status--ok").count() == 1, session.where("status: synced")
    assert page.locator(".day", has_text="Today").count() >= 1, session.where("day headings")
    total = page.locator(".figure__main").first.inner_text()
    assert total.startswith("$"), session.where(f"portfolio total {total!r}")


@viewports(DESKTOP, TABLET, MOBILE, SMALL)
def test_the_dashboard_has_no_horizontal_overflow(session, viewport):
    page = session.open("/")
    extra = overflow(page, viewport)
    assert extra <= 0, session.where(f"dashboard is {extra}px wider than the screen")


@viewports(MOBILE, SMALL)
def test_a_feed_without_the_scam_row_fits_a_phone(session, viewport):
    """Kept from when U-03 was open, where it scoped the defect to its cause;
    a narrower page than the dashboard, and a cheap check."""
    page = session.open(f"/?wallet={session.stand.account_id('Main')}")
    extra = overflow(page, viewport)
    assert extra <= 0, session.where(f"Main's feed is {extra}px wider than the screen")


# ------------------------------------------------------ scopes and filters


@viewports(DESKTOP)
def test_switching_wallets_and_scopes(session, viewport):
    page = session.open("/")
    all_rows = rows(page).count()

    page.locator(".wallet__hit", has_text="Cold").click()
    wait_for_feed(page)
    cold = session.stand.account_id("Cold")
    assert page.locator("#wallet-value").input_value() == str(cold), session.where("select Cold")
    assert f"wallet={cold}" in page.url, session.where("the URL follows the scope")
    assert 0 < rows(page).count() < all_rows, session.where("Cold's feed is narrower")

    page.locator(".wallets__group", has_text="Watching").click()
    wait_for_feed(page)
    assert "scope=watched" in page.url, session.where("WATCHING scope")
    assert rows(page).count() > 0, session.where("the watched wallet has rows")

    page.locator(".wallets__group--all").click()
    wait_for_feed(page)
    assert rows(page).count() == all_rows, session.where("back to everything")


@viewports(MOBILE)
def test_choosing_a_wallet_from_the_scope_sheet(session, viewport):
    page = session.open("/")
    page.locator(".topline__scope").click()
    assert "scope-open" in (page.locator("html").get_attribute("class") or ""), \
        session.where("the scope sheet opens")
    page.locator(".wallet__hit", has_text="Payments").click()
    wait_for_feed(page)
    assert page.locator(".topline__scope").inner_text().startswith("Payments"), \
        session.where("the top bar names the scope")
    assert "scope-open" not in (page.locator("html").get_attribute("class") or ""), \
        session.where("the sheet closes after choosing")


@viewports(DESKTOP)
def test_search_and_filters(session, viewport):
    page = session.open("/")

    page.locator("#search").press_sequentially("Binance", delay=20)
    page.wait_for_timeout(600)          # the search waits 300 ms for typing to stop
    wait_for_feed(page)
    found = rows(page)
    assert found.count() >= 1, session.where("search by label")
    for text in found.all_inner_texts():
        assert "Binance" in text, session.where(f"search result without the label: {text!r}")

    page.locator("#search").fill("")
    page.locator("#search").press("Backspace")
    page.wait_for_timeout(600)
    wait_for_feed(page)

    page.locator(".segment--in").click()
    wait_for_feed(page)
    directions = page.locator(f"{ROWS} .dir").all_inner_texts()
    assert directions and set(directions) == {"IN"}, session.where(f"IN filter: {set(directions)}")

    page.locator(".segment--all").click()
    page.locator("select[name=asset]").select_option(label="USDC")
    wait_for_feed(page)
    symbols = {s.strip() for s in page.locator(f"{ROWS} .cell-value .sym").all_inner_texts()}
    assert symbols == {"USDC"}, session.where(f"asset filter: {symbols}")


@viewports(DESKTOP)
@known("U-10", "search reacts to keyup only; pasted text does nothing until blur")
def test_pasting_into_search_filters_the_feed(session, viewport):
    page = session.open("/")
    before = rows(page).count()
    page.locator("#search").fill("Binance")   # an input event with no keyup, as a paste
    page.wait_for_timeout(800)
    assert rows(page).count() < before, session.where("pasted search applied")


# --------------------------------------------------------------- inspector


def open_the_swap(session, path: str):
    page = session.open(path)
    swap = page.locator(f'{ROWS}[data-tx="{tx_hash(SWAP_SEED)}"]').first
    swap.scroll_into_view_if_needed()
    # The time cell: the counterparty and the hash are links of their own,
    # which by design do not open the inspector.
    swap.locator(".cell-time").click()
    page.wait_for_selector("html.inspector-open", state="attached", timeout=5000)
    return page


def close_button_on_screen(page, viewport) -> bool:
    box = page.locator("#inspector-body .insp__close").bounding_box()
    return box is not None and box["x"] + box["width"] <= viewport.width


@viewports(DESKTOP, MOBILE)
def test_the_inspector_opens_and_closes(session, viewport):
    # Main's feed: the swap is Main's, and the page has no scam row (U-03).
    page = open_the_swap(session, f"/?wallet={session.stand.account_id('Main')}")
    body = page.locator("#inspector-body")
    assert body.locator(".insp").count() == 1, session.where("inspector content")
    assert "fee" in body.inner_text().lower(), session.where("the fee is read on demand")

    if viewport.mobile:
        on_screen = close_button_on_screen(page, viewport)
        assert on_screen, session.where("the close button is on screen")
        body.locator(".insp__close").click()
    else:
        page.keyboard.press("Escape")
    page.wait_for_function("() => !document.documentElement.classList.contains('inspector-open')")


@viewports(MOBILE)
def test_the_inspector_close_button_is_on_screen_on_the_dashboard(session, viewport):
    page = open_the_swap(session, "/")
    assert close_button_on_screen(page, viewport), session.where("the close button is on screen")


@viewports(DESKTOP)
@known("U-06", "feed rows are not focusable; the inspector opens by mouse only")
def test_the_inspector_opens_from_the_keyboard(session, viewport):
    page = session.open("/")
    first = rows(page).first
    first.focus()
    focused = page.evaluate("() => document.activeElement?.matches('tr[data-activity]')")
    assert focused, session.where("a feed row takes keyboard focus")
    page.keyboard.press("Enter")
    page.wait_for_selector("html.inspector-open", state="attached", timeout=2000)


# ------------------------------------------------------ mobile holdings sheet


@viewports(MOBILE)
def test_the_holdings_sheet_opens(session, viewport):
    page = session.open("/")
    page.locator(".holdings__more").first.click()
    page.wait_for_selector("#wallet-dialog[open]", timeout=5000)
    assert page.locator("#wallet-dialog .holding").count() > 0, \
        session.where("the sheet carries the holdings")


@viewports(MOBILE)
def test_the_holdings_sheet_shows_its_assets(session, viewport):
    page = session.open("/")
    page.locator(".holdings__more").first.click()
    page.wait_for_selector("#wallet-dialog[open]", timeout=5000)
    visible = page.locator("#wallet-dialog .holding:visible").count()
    assert visible > 0, session.where("holdings visible in the sheet")


# ------------------------------------------------ changes (each on a fresh stand)


@viewports(DESKTOP)
def test_older_history_is_queued_then_running_then_completed(fresh_session, viewport):
    session = fresh_session
    provider = session.stand.provider
    page = session.open(f"/?wallet={session.stand.account_id('Main')}")
    row = page.locator("#history-row")
    assert "Load 30 more days" in row.inner_text(), session.where("end of feed")
    assert page.locator(f'[data-tx="{tx_hash("ui-ancient")}"]').count() == 0

    provider.gate.clear()                     # hold the deep fetch
    try:
        row.locator("button").click()
        page.wait_for_selector("#history-row.is-busy", timeout=3000)
        assert "Queued" in page.locator("#history-row").inner_text(), session.where("queued")
        page.wait_for_function(
            "() => document.querySelector('#history-row')?.innerText.includes('Indexing')",
            timeout=8000,
        )
    finally:
        provider.gate.set()
    page.wait_for_selector(f'[data-tx="{tx_hash("ui-ancient")}"]', timeout=10000)
    assert page.locator("#history-row.is-busy").count() == 0, session.where("polling stopped")
    assert session.stand.db.get_sync_status("ethereum").history_status == "completed"


@viewports(DESKTOP)
def test_removing_a_wallet_updates_the_page(fresh_session, viewport):
    session = fresh_session
    stand = session.stand
    page = session.open("/")
    response = page.request.post(stand.url + "/wallets", form={
        "name": "Removable", "address": REMOVABLE, "chain": "ethereum", "owned": "on",
    })
    assert response.ok, session.where("adding a wallet")
    page.request.post(stand.url + "/__stand__/sync")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if stand.db.get_sync_status("ethereum").activity_count > 100:
            break
        time.sleep(0.1)
    page.reload()
    wait_for_feed(page)
    assert page.locator(".wallet__name", has_text="Removable").count() == 1
    page.locator(".wallet__hit", has_text="Removable").click()
    wait_for_feed(page)
    assert rows(page).count() >= 1, session.where("its history is indexed")

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('summary[aria-label="Actions for Removable"]').click()
    page.locator(".wallet__menu[open] .menu__danger").click()
    page.wait_for_function("() => !document.querySelector('#wallet-value').value", timeout=5000)
    wait_for_feed(page)
    assert page.locator(".wallet__name", has_text="Removable").count() == 0, \
        session.where("gone from the sidebar")
    assert "Removable" not in page.locator("#feed-body").inner_text(), \
        session.where("gone from the feed")
    assert rows(page).count() >= 20, session.where("back to every wallet")


# ------------------------------------------------------------ accessibility


# The rules axe-core reports today, per viewport (audit U-07 and U-06). The
# comparison is exact: a new rule fails the run, and so does a fixed one, so
# this list shrinks as the defects are fixed instead of quietly going stale.
KNOWN_AXE = {
    "desktop": {"color-contrast", "region", "empty-table-header"},
    # The table header is hidden on a phone, so empty-table-header is not.
    "mobile": {"color-contrast", "region", "select-name"},
}


@viewports(DESKTOP, MOBILE)
def test_accessibility_matches_the_known_baseline(session, viewport):
    page = session.open("/")
    found = run_axe(page)
    expected = KNOWN_AXE[viewport.name]
    new = sorted(set(found) - expected)
    fixed = sorted(expected - set(found))
    detail = ", ".join(f"{rule} ({impact}, {count} nodes)" for rule, (impact, count)
                       in sorted(found.items()))
    assert not new, session.where(f"new axe violations: {new}; all: {detail}")
    assert not fixed, session.where(
        f"known violations no longer reported, update KNOWN_AXE: {fixed}; all: {detail}"
    )


@viewports(DESKTOP)
def test_the_main_controls_are_reachable_by_keyboard(session, viewport):
    page = session.open("/")
    reached = []
    for _ in range(120):
        page.keyboard.press("Tab")
        reached.append(page.evaluate(
            "() => { const e = document.activeElement; return e ? "
            "(e.id || e.name || e.className || e.tagName) + '|' + (e.innerText || e.value || '')"
            ".trim().slice(0, 30) : '' }"
        ))
    joined = "\n".join(reached)
    for control, pattern in [
        ("search box", r"^search\|"),
        ("direction filter", r"^direction\|"),
        ("asset filter", r"^asset\|"),
        ("a wallet in the sidebar", r"^wallet__hit\|Main"),
        ("add a wallet", r"add-wallet-link\|\+ Add wallet"),
    ]:
        reached_it = re.search(pattern, joined, re.MULTILINE)
        assert reached_it, session.where(f"Tab never reaches the {control}")
