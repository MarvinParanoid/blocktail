"""Phones and money (audit U-02, U-03, U-04, U-05).

* U-02: the holdings sheet on a phone opened empty.
* U-03: long content — the scam token's 34-digit amount, long wallet names —
  made the page wider than the screen, and pushed the inspector's close
  button off it.
* U-04: amounts were cut short with an ellipsis ("1,000,0…"), which hides
  their magnitude.
* U-05: a quote the total refused to believe must not be printed anywhere.

The rule these hold the page to: a number is never cut. A very large one may
be shown compactly ("1.0×10³⁰", "12.3B"), and its exact value is then
always one step away without a mouse — in the inspector for a transfer, in
the holdings sheet for a holding — and in the text a screen reader reads.
"""

from __future__ import annotations

import re

import pytest
from conftest import DESKTOP, MOBILE, SMALL, TABLET, Viewport, viewports

from tests.conftest import tx_hash

WIDE = Viewport("wide", 430, 932, True)
NARROW = Viewport("narrow", 360, 760, True)
ALL_WIDTHS = [DESKTOP, TABLET, WIDE, MOBILE, NARROW, SMALL]
PHONES = [WIDE, MOBILE, NARROW, SMALL]
ROWS = "#feed-body tr[data-activity]"
NMT_EXACT = "1,000,000,000,000,000,000,000,000,000,000"


def overflow(page, viewport) -> int:
    return page.evaluate("() => document.documentElement.scrollWidth") - viewport.width


def cut_numbers(page) -> list[str]:
    """Visible numbers whose text does not fit their box: an ellipsis, or
    clipped digits."""
    # Inline spans have no scroll box of their own, so the boxes that clip
    # them are checked too: the feed's amount cell and the inspector's line.
    return page.evaluate("""() => [...document.querySelectorAll(
        '.num, .amount, .fiat, .holding__amount, .holding__value, .figure__main,'
        + ' .feed td.cell-value, .xfer__amount, .insp__cost dd')]
      .filter(e => e.offsetParent !== null && getComputedStyle(e).display !== 'inline'
                   && e.scrollWidth > e.clientWidth + 1)
      .map(e => (e.className || e.tagName) + ': ' + e.textContent.trim().slice(0, 40))""")


def open_row(session, seed: str, path: str = "/?dust=show"):
    page = session.open(path)
    row = page.locator(f'{ROWS}[data-tx="{tx_hash(seed)}"]').first
    row.scroll_into_view_if_needed()
    row.locator(".cell-time").click()
    page.wait_for_selector("html.inspector-open", state="attached", timeout=5000)
    return page, row


# ------------------------------------------------------- U-02: holdings sheet


@viewports(TABLET, MOBILE, SMALL)
def test_the_holdings_sheet_lists_the_holdings(session, viewport):
    page = session.open("/")
    page.locator(".holdings__more").first.click()
    page.wait_for_selector("#wallet-dialog[open]", timeout=5000)
    visible = page.locator("#wallet-dialog .holding:visible")
    assert visible.count() >= 5, session.where(f"{visible.count()} holdings visible")
    text = page.locator("#wallet-dialog").inner_text()
    assert "USDC" in text and "LINK" in text, session.where("named holdings")
    nmt = page.locator("#wallet-dialog .holding", has_text="NMT")
    assert "excluded" in nmt.inner_text(), session.where("NMT says excluded, not a quote")

    sheet = page.locator("#wallet-dialog")
    if page.evaluate("(d) => d.scrollHeight > d.clientHeight", sheet.element_handle()):
        page.evaluate("(d) => { d.scrollTop = d.scrollHeight }", sheet.element_handle())
        assert page.evaluate("(d) => d.scrollTop > 0", sheet.element_handle()), \
            session.where("the sheet scrolls")
    assert overflow(page, viewport) <= 0, session.where("the open sheet fits")

    sheet.locator(".insp__close").click()
    page.wait_for_function("() => !document.querySelector('#wallet-dialog').open")


@viewports(MOBILE, SMALL)
def test_choosing_a_holding_filters_the_feed(session, viewport):
    page = session.open("/")
    page.locator(".holdings__more").first.click()
    page.wait_for_selector("#wallet-dialog[open]", timeout=5000)
    with page.expect_response(lambda r: "/activity?asset=" in r.url):
        page.locator("#wallet-dialog .holding", has_text="USDC").first.click()
    page.wait_for_function("() => !document.querySelector('#wallet-dialog').open")
    page.wait_for_timeout(200)
    symbols = {s.strip() for s in page.locator(f"{ROWS} .cell-value .sym").all_inner_texts()}
    assert symbols == {"USDC"}, session.where(f"feed after choosing USDC: {symbols}")


# ------------------------------------------------------------- U-03: overflow


PAGES = {
    "dashboard": "/",
    "everything shown": "/?dust=show",
    "the scam token's wallet": "/?wallet={Trading}&dust=show",
    "the long-named wallet":
        "/?wallet={Treasury multisig — operations reserve (legacy Safe, 2021)}",
}


def resolve(stand, path: str) -> str:
    return re.sub(r"\{([^}]+)\}", lambda m: str(stand.account_id(m.group(1))), path)


@pytest.mark.parametrize("where", list(PAGES))
@viewports(*ALL_WIDTHS)
def test_no_page_is_wider_than_the_screen(session, viewport, where):
    page = session.open(resolve(session.stand, PAGES[where]))
    extra = overflow(page, viewport)
    assert extra <= 0, session.where(f"{where}: {extra}px wider than the screen")


@viewports(*PHONES)
def test_the_inspector_closes_on_every_phone(session, viewport):
    page, _ = open_row(session, "ui-nmt")
    assert overflow(page, viewport) <= 0, session.where("the open inspector fits")
    close = page.locator("#inspector-body .insp__close")
    box = close.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= viewport.width, \
        session.where(f"the close button is on screen: {box}")
    close.click()
    page.wait_for_function("() => !document.documentElement.classList.contains('inspector-open')")


# --------------------------------------------- U-04: no number is ever cut


@pytest.mark.parametrize("state", ["feed", "inspector open"])
@viewports(DESKTOP, Viewport("laptop", 1024, 768, False), TABLET, MOBILE, SMALL)
def test_no_number_is_cut_short(session, viewport, state):
    page = session.open("/?dust=show")
    if state == "inspector open":
        page, _ = open_row(session, "ui-nmt")
    cut = cut_numbers(page)
    assert not cut, session.where(f"{state}: numbers cut short: {cut}")


@viewports(MOBILE, SMALL)
def test_the_exact_amount_is_one_tap_away_on_a_phone(session, viewport):
    page, row = open_row(session, "ui-nmt")
    shown = row.locator(".amount").inner_text()
    assert shown != NMT_EXACT and "10" in shown, session.where(f"the row is compact: {shown!r}")
    assert NMT_EXACT in row.inner_text() or NMT_EXACT in row.text_content(), \
        session.where("the exact value is in the row's text for a screen reader")
    inspector = page.locator("#inspector-body")
    exact = inspector.locator(".xfer .num", has_text=NMT_EXACT)
    assert exact.count() >= 1, session.where("the inspector shows it exactly")
    assert exact.first.is_visible()
    assert not cut_numbers(page), session.where("and nothing in it is cut")


@viewports(MOBILE)
def test_the_sheet_shows_a_holding_exactly(session, viewport):
    page = session.open(f"/?wallet={session.stand.account_id('Main')}")
    page.locator(".holdings__more").first.click()
    page.wait_for_selector("#wallet-dialog[open]", timeout=5000)
    usdc = page.locator("#wallet-dialog .holding", has_text="USDC").first
    assert "12,500" in usdc.inner_text(), session.where(usdc.inner_text())


# ------------------------------------------------ U-05 and unknown decimals


@viewports(DESKTOP, MOBILE)
def test_unknown_decimals_are_raw_units_and_never_valued(session, viewport):
    page, row = open_row(session, "ui-mystery")
    assert row.locator(".kind", has_text="raw").count() == 1, session.where("marked raw")
    assert row.locator(".fiat").count() == 0, session.where("no USD for unknown decimals")
    inspector = page.locator("#inspector-body").inner_text().lower()
    assert "raw" in inspector, session.where("the inspector says raw too")
    assert "$" not in page.locator("#inspector-body .xfer", has_text="MYSTERY").inner_text()


@viewports(DESKTOP, MOBILE)
def test_an_excluded_asset_shows_no_made_up_value(session, viewport):
    page, row = open_row(session, "ui-nmt")
    assert row.locator(".fiat").count() == 0, session.where("no USD on the feed row")
    assert "$420" not in page.locator("#inspector-body").inner_text(), session.where("inspector")
    assert "$420" not in page.locator(".summary-panel").inner_text(), session.where("summary")
    forged = page.locator(f'{ROWS}[data-tx="{tx_hash("ui-forged")}"]')
    assert forged.count() == 1 and forged.locator(".fiat").count() == 0, \
        session.where("a forged ticker's pool price is not printed either")


# ------------------------------------------------------ drawers across resizes


@pytest.mark.parametrize("drawer", ["scope sheet", "inspector", "holdings sheet"])
@viewports(MOBILE)
def test_a_drawer_survives_a_resize_to_desktop_and_back(session, viewport, drawer):
    page = session.open("/")
    if drawer == "scope sheet":
        page.locator(".topline__scope").click()
    elif drawer == "inspector":
        page, _ = open_row(session, "ui-main-to-cold", "/")
    else:
        page.locator(".holdings__more").first.click()
        page.wait_for_selector("#wallet-dialog[open]")

    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(300)
    scrim = page.locator(".scrim")
    assert not scrim.is_visible(), session.where(f"{drawer}: no scrim left over a desktop page")
    assert page.evaluate("() => document.documentElement.scrollWidth") <= 1440
    if drawer != "holdings sheet":
        page.locator(".wallet__hit", has_text="Cold").click()   # the page is usable
        page.wait_for_load_state("networkidle")

    page.set_viewport_size({"width": viewport.width, "height": viewport.height})
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.documentElement.scrollWidth") <= viewport.width, \
        session.where(f"{drawer}: back on a phone, still fits")
