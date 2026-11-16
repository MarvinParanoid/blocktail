"""Keyboard, focus, contrast and labels (audit U-06, U-07; S6).

Keyboard model for the feed: the feed is one stop in the Tab order, not one
per row. Each row has a real button (its time) that opens the transaction;
only one of them is tabbable at a time, and the arrow keys, Home and End move
between rows. Enter and Space press the button.

The inspector is a panel beside the feed on a wide screen — not a modal; the
point is to keep reading what it inspects — and a modal sheet on a phone. The
scope sheet and the holdings sheet are modal too. A modal: focus goes in, Tab
cannot leave it, the page behind is inert, Escape closes it, and focus goes
back to what opened it.
"""

from __future__ import annotations

import pytest
from conftest import DESKTOP, MOBILE, SMALL, TABLET, run_axe, viewports

from tests.conftest import tx_hash

ROWS = "#feed-body tr[data-activity]"
NMT_EXACT = "1,000,000,000,000,000,000,000,000,000,000"


def active(page) -> dict:
    return page.evaluate("""() => { const e = document.activeElement;
        const row = e && e.closest && e.closest('tr[data-activity]');
        return {tag: e && e.tagName, cls: e && e.className && e.className.toString(),
                row: row ? row.dataset.activity : null,
                inInspector: !!(e && e.closest && e.closest('#inspector')),
                inSidebar: !!(e && e.closest && e.closest('#sidebar')),
                inDialog: !!(e && e.closest && e.closest('#wallet-dialog')),
                text: e && (e.innerText || e.value || '').trim().slice(0, 40)} }""")


def tab_to_feed(page, limit: int = 80) -> dict:
    """Press Tab until a feed row's button has focus."""
    for _ in range(limit):
        page.keyboard.press("Tab")
        where = active(page)
        if where["row"] and "row__open" in (where["cls"] or ""):
            return where
    raise AssertionError(f"Tab never reached the feed in {limit} presses")


def inspector_open(page) -> bool:
    return page.evaluate("() => document.documentElement.classList.contains('inspector-open')")


# ------------------------------------------------------------- the feed


@pytest.mark.parametrize("key", ["Enter", " "], ids=["enter", "space"])
@viewports(DESKTOP, MOBILE)
def test_the_inspector_opens_from_the_keyboard_alone(session, viewport, key):
    page = session.open("/")
    row = tab_to_feed(page)["row"]
    page.keyboard.press(key)
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.wait_for_selector(f'#inspector-body .insp[data-selected="{row}"]')
    assert active(page)["inInspector"], session.where("focus moves into the inspector")


@viewports(DESKTOP)
def test_the_feed_is_one_tab_stop_and_arrows_move_within_it(session, viewport):
    page = session.open("/")
    first = tab_to_feed(page)
    stops = page.evaluate("""() => {
        const rows = new Set([...document.querySelectorAll(
            '#feed-body a[href], #feed-body button')]
          .filter(e => e.tabIndex >= 0 && e.offsetParent !== null)
          .map(e => (e.closest('tr[data-activity]') || e.closest('tr')).dataset.activity
                    || 'fold-or-end'));
        return [...rows]; }""")
    real = [r for r in stops if r != "fold-or-end"]
    assert real == [first["row"]], session.where(f"tabbable rows in the feed: {stops}")
    page.keyboard.press("ArrowDown")
    second = active(page)
    assert second["row"] and second["row"] != first["row"], session.where("ArrowDown")
    page.keyboard.press("ArrowUp")
    assert active(page)["row"] == first["row"], session.where("ArrowUp")
    page.keyboard.press("End")
    last = active(page)["row"]
    page.keyboard.press("Home")
    assert active(page)["row"] == first["row"] and last != first["row"], session.where("Home/End")
    # Tab walks the current row's own controls (its links, its fold-out),
    # then leaves the feed — a handful of presses, not hundreds.
    path = []
    for presses in range(1, 9):
        page.keyboard.press("Tab")
        here = active(page)
        path.append(f"{here['row']}:{here['cls']}")
        if here["row"] is None and not page.evaluate(
                "() => !!document.activeElement.closest('#feed-body')"):
            break
    else:
        raise AssertionError(session.where(f"Tab never leaves the feed: {path}"))
    assert presses <= 6, session.where(f"{presses} presses to leave the feed: {path}")


@viewports(DESKTOP, MOBILE)
def test_closing_the_inspector_returns_focus_to_the_row(session, viewport):
    page = session.open("/")
    tab_to_feed(page)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    row = active(page)["row"]
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.documentElement.classList.contains('inspector-open')")
    where = active(page)
    assert where["row"] == row and "row__open" in where["cls"], session.where(f"focus on {where}")


@viewports(DESKTOP)
def test_a_feed_refresh_keeps_the_focused_row(session, viewport):
    page = session.open("/")
    tab_to_feed(page)
    for _ in range(3):
        page.keyboard.press("ArrowDown")
    row = active(page)["row"]
    with page.expect_response(lambda r: "/activity" in r.url):
        page.evaluate("() => document.body.dispatchEvent(new Event('refreshFeed'))")
    page.wait_for_timeout(200)
    where = active(page)
    assert where["row"] == row and "row__open" in where["cls"], \
        session.where(f"after the swap, focus is on {where}")
    tabbable = page.evaluate(
        "() => document.querySelectorAll('#feed-body .row__open[tabindex=\"0\"]').length")
    assert tabbable == 1, session.where("still one row in the Tab order")


# ------------------------------------------------------------ modals


LIVE = ("() => !['.topbar', '#summary', '.feed-panel']"
        ".some(s => document.querySelector(s).closest('[inert]'))")


def assert_trapped(session, page, region: str, presses: int = 25):
    for key in ("Tab", "Shift+Tab"):
        for _ in range(presses):
            page.keyboard.press(key)
            where = active(page)
            assert where[region], session.where(f"{key} left the {region}: {where}")


def background_tab_targets(page, container: str) -> list[str]:
    """Focusable, visible, not inert, and outside the open modal."""
    return page.evaluate("""(sel) => {
        const box = document.querySelector(sel);
        const q = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';
        return [...document.querySelectorAll(q)].filter(e =>
            !box.contains(e) && !e.closest('[inert]') && !e.disabled &&
            e.offsetParent !== null && getComputedStyle(e).visibility !== 'hidden' &&
            e.tabIndex >= 0).map(e => (e.className || e.tagName).toString().slice(0, 30));
    }""", container)


@viewports(MOBILE, SMALL)
def test_the_inspector_sheet_is_a_modal_on_a_phone(session, viewport):
    page = session.open("/")
    tab_to_feed(page)
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.wait_for_timeout(250)
    inspector = page.locator("#inspector")
    assert inspector.get_attribute("role") == "dialog", session.where("role")
    assert inspector.get_attribute("aria-modal") == "true", session.where("aria-modal")
    label = page.evaluate("() => document.getElementById("
                          "document.getElementById('inspector').getAttribute('aria-labelledby'))"
                          "?.textContent.trim()")
    assert label and "Transaction" in label, session.where(f"labelled by {label!r}")
    assert not background_tab_targets(page, "#inspector"), session.where("background reachable")
    assert_trapped(session, page, "inInspector")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.documentElement.classList.contains('inspector-open')")
    assert active(page)["row"], session.where("focus back on the row")
    assert page.evaluate(LIVE), session.where("the page behind is live again")


@viewports(DESKTOP)
def test_the_desktop_inspector_is_a_labelled_panel_not_a_modal(session, viewport):
    page = session.open("/")
    tab_to_feed(page)
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    inspector = page.locator("#inspector")
    assert inspector.get_attribute("aria-modal") != "true", session.where("not modal on desktop")
    assert inspector.get_attribute("aria-labelledby"), session.where("labelled")
    assert page.evaluate(LIVE), session.where("the feed stays live beside the panel")


@viewports(MOBILE, SMALL)
def test_the_scope_sheet_is_a_modal_on_a_phone(session, viewport):
    page = session.open("/")
    page.locator(".topline__scope").focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('scope-open')")
    page.wait_for_timeout(250)
    sidebar = page.locator("#sidebar")
    assert sidebar.get_attribute("role") == "dialog" and sidebar.get_attribute("aria-modal") == "true"
    assert active(page)["inSidebar"], session.where("focus moves into the sheet")
    assert not background_tab_targets(page, "#sidebar"), session.where("background reachable")
    assert_trapped(session, page, "inSidebar")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.documentElement.classList.contains('scope-open')")
    assert "topline__scope" in (active(page)["cls"] or ""), session.where("focus back on the opener")


@viewports(MOBILE, DESKTOP)
def test_the_holdings_sheet_is_a_modal(session, viewport):
    page = session.open("/")
    opener = page.locator(".holdings__more").first
    opener.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#wallet-dialog[open]")
    labelled = page.evaluate("() => { const d = document.getElementById('wallet-dialog');"
                             " const t = document.getElementById(d.getAttribute('aria-labelledby'));"
                             " return t ? t.textContent.trim() : null }")
    assert labelled, session.where("the dialog is labelled by its title")
    assert active(page)["inDialog"], session.where("focus moves into the sheet")
    assert_trapped(session, page, "inDialog")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.getElementById('wallet-dialog').open")
    assert "holdings__more" in (active(page)["cls"] or ""), session.where("focus back on the opener")


@viewports(MOBILE)
def test_the_inspector_and_a_sheet_do_not_fight(session, viewport):
    page = session.open("/")
    tab_to_feed(page)
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.keyboard.press("Escape")
    page.locator(".topline__scope").click()
    page.wait_for_function("() => document.documentElement.classList.contains('scope-open')")
    assert not inspector_open(page)
    page.keyboard.press("Escape")
    assert page.evaluate(LIVE), session.where("the page behind is live")


@viewports(MOBILE)
def test_resizing_with_the_inspector_open_moves_between_modal_and_panel(session, viewport):
    page = session.open("/")
    tab_to_feed(page)
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.wait_for_timeout(200)
    assert page.locator("#inspector").get_attribute("aria-modal") == "true"
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(300)
    assert page.locator("#inspector").get_attribute("aria-modal") != "true", \
        session.where("a docked panel on desktop")
    assert page.evaluate(LIVE), session.where("the page is live on desktop")
    page.set_viewport_size({"width": viewport.width, "height": viewport.height})
    page.wait_for_timeout(300)
    assert page.locator("#inspector").get_attribute("aria-modal") == "true", \
        session.where("modal again on the phone")


# -------------------------------------------- exact figures, labels


@viewports(DESKTOP)
def test_exact_holdings_are_reachable_on_desktop_without_hover(session, viewport):
    page = session.open("/")
    opener = page.get_by_role("button", name="All holdings")
    assert opener.is_visible(), session.where("a visible way in on a wide screen")
    opener.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#wallet-dialog[open]")
    figure = page.locator("#wallet-dialog .holding__amount", has_text=NMT_EXACT)
    assert figure.count() == 1 and figure.is_visible(), session.where("the exact amount is shown")
    selected = page.evaluate("""(text) => {
        const el = [...document.querySelectorAll('#wallet-dialog .holding__amount')]
            .find(e => e.textContent.includes(text));
        if (el.closest('a')) return 'inside a link';
        if (getComputedStyle(el).userSelect === 'none') return 'not selectable';
        const range = document.createRange(); range.selectNodeContents(el);
        const s = getSelection(); s.removeAllRanges(); s.addRange(range);
        return s.toString().trim(); }""", NMT_EXACT)
    assert selected == NMT_EXACT, session.where(f"select and copy: {selected!r}")


@viewports(DESKTOP, MOBILE)
def test_an_excluded_asset_and_a_lookalike_ticker_are_labelled_in_words(session, viewport):
    page = session.open("/?dust=show")
    nmt = page.locator(f'{ROWS}[data-tx="{tx_hash("ui-nmt")}"]')
    assert "not valued" in nmt.inner_text().lower(), session.where("feed: excluded")
    forged = page.locator(f'{ROWS}[data-tx="{tx_hash("ui-forged")}"]')
    assert "unverified" in forged.inner_text().lower(), session.where("feed: lookalike")
    assert "scam" not in (nmt.inner_text() + forged.inner_text()).lower(), \
        session.where("no accusation the app cannot prove")

    nmt.locator(".row__open").click()
    page.wait_for_selector('#inspector-body .insp[data-selected]')
    assert "not valued" in page.locator("#inspector-body").inner_text().lower(), \
        session.where("inspector: excluded")
    page.keyboard.press("Escape")

    page.locator(".holdings__more").first.click()
    page.wait_for_selector("#wallet-dialog[open]")
    sheet = page.locator("#wallet-dialog")
    assert "not valued" in sheet.locator(".holding", has_text="NMT").inner_text().lower()
    assert "unverified" in sheet.locator(".holding", has_text="10,000").first.inner_text().lower(), \
        session.where("sheet: lookalike")


# ---------------------------------------------------------------- axe


@pytest.mark.parametrize("scheme", ["dark", "light"])
@viewports(DESKTOP, TABLET, MOBILE, SMALL)
def test_no_axe_violations(session, viewport, scheme):
    session.page.emulate_media(color_scheme=scheme)
    page = session.open("/?dust=show")
    found = run_axe(page)
    assert not found, session.where(f"{scheme}: axe violations: {found}")


@pytest.mark.parametrize("scheme", ["dark", "light"])
@viewports(MOBILE)
def test_no_axe_violations_with_a_sheet_open(session, viewport, scheme):
    session.page.emulate_media(color_scheme=scheme)
    page = session.open("/")
    tab_to_feed(page)
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.wait_for_timeout(250)
    found = run_axe(page)
    assert not found, session.where(f"{scheme}, inspector open: {found}")


@viewports(DESKTOP)
def test_both_colour_schemes_are_honoured(session, viewport):
    page = session.page
    backgrounds = {}
    for scheme in ("dark", "light"):
        page.emulate_media(color_scheme=scheme)
        session.open("/")
        backgrounds[scheme] = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        declared = page.evaluate("() => getComputedStyle(document.documentElement).colorScheme")
        assert scheme in declared, session.where(f"{scheme}: color-scheme is {declared!r}")
    assert backgrounds["dark"] != backgrounds["light"], session.where(f"{backgrounds}")


@pytest.mark.parametrize("scheme", ["dark", "light"])
@viewports(DESKTOP)
def test_no_axe_violations_with_the_inspector_beside_the_feed(session, viewport, scheme):
    """The audit counted a heading-order violation here on top of the
    dashboard's."""
    session.page.emulate_media(color_scheme=scheme)
    page = session.open("/")
    tab_to_feed(page)
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.wait_for_timeout(250)
    found = run_axe(page)
    assert not found, session.where(f"{scheme}, inspector open: {found}")


# ------------------------------------------------- U-11: the add-wallet form


@viewports(DESKTOP)
def test_a_refused_wallet_puts_focus_on_the_field_to_fix(session, viewport):
    # The refusal is a 422 by design, and the browser logs every 4xx.
    session.expect_console_errors(r"status of 422")
    page = session.open("/")
    page.locator(".add-wallet-link").click()
    page.wait_for_selector("#wallet-dialog[open] input[name=address]")
    page.fill("#wallet-dialog input[name=name]", "Typo")
    page.fill("#wallet-dialog input[name=address]", "0x1234")
    page.keyboard.press("Enter")
    page.wait_for_selector("#wallet-dialog .sheet__error")
    page.wait_for_function("() => document.activeElement.name === 'address'", timeout=2000)

    error = page.locator("#wallet-error")
    assert error.get_attribute("role") == "alert", session.where("the refusal is announced")
    focused = page.evaluate("""() => { const e = document.activeElement;
        return [e.name, e.getAttribute('aria-invalid'), e.getAttribute('aria-describedby')] }""")
    assert focused == ["address", "true", "wallet-error"], session.where(f"focus on {focused}")
    assert page.locator("#wallet-dialog input[name=name]").get_attribute("aria-invalid") is None
    found = run_axe(page)
    assert not found, session.where(f"refused form: {found}")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('#wallet-dialog').open")
