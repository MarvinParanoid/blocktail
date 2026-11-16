"""Filters, search and the page's own events (S7: U-08, U-10, U-12 and the
removed-wallet link), and the skip link.

What these hold the page to:

* search follows every way text gets into the box — typing, Ctrl+V, the
  context menu's Paste, the native clear — once the typing pauses, and the
  feed never shows the answer to a question that is no longer asked;
* every way of changing the feed (the form, the sidebar, the summary, a
  counterparty) goes through one request path, so what depends on the
  filters (the address bar, the hidden-transfers note) follows all of them;
* a wallet change updates the page in one fixed order, decided on the page:
  nothing asks the server about a wallet that has just been removed;
* a link to a wallet that is no longer watched says so, rather than showing an
  empty feed under "All activity";
* pages loaded by scrolling survive the status poll;
* the keyboard reaches the feed from the top of the page in two presses.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import DESKTOP, MOBILE, SCRIPT, SMALL, TABLET, run_axe, viewports
from stand import REMOVABLE

from tests.conftest import tx_hash

ROWS = "#feed-body tr[data-activity]"


def rows(page):
    return page.locator(ROWS)


def record_requests(page) -> list[str]:
    """Every GET the page sends to the stand from now on, as path?query."""
    seen: list[str] = []

    def on_request(request):
        if request.method != "GET":
            return
        url = urlparse(request.url)
        if not url.path.startswith(("/static", "/__stand__")):
            seen.append(url.path + ("?" + url.query if url.query else ""))

    page.on("request", on_request)
    return seen


def query(path: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}


def settle(page, ms: int = 700):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(ms)


def symbols(page) -> set[str]:
    return {s.strip() for s in page.locator(f"{ROWS} .cell-value .sym").all_inner_texts()}


def open_search(page, viewport):
    if viewport.mobile:
        page.locator(".field-toggle--open").click()
    return page.locator("#search")


# ------------------------------------------------------------ U-10: search


@viewports(DESKTOP, MOBILE)
def test_pasting_with_the_keyboard_searches(session, viewport):
    session.context.grant_permissions(["clipboard-read", "clipboard-write"],
                                      origin=session.stand.url)
    page = session.open("/")
    before = rows(page).count()
    box = open_search(page, viewport)
    page.evaluate("() => navigator.clipboard.writeText('Binance')")
    box.focus()
    page.keyboard.press("Control+V")
    settle(page)
    assert box.input_value() == "Binance"
    assert 0 < rows(page).count() < before, session.where("Ctrl+V applied the search")
    assert query(page.url).get("q") == "Binance", session.where(page.url)


@viewports(DESKTOP)
def test_pasting_from_the_context_menu_searches(session, viewport):
    """The context menu's Paste is an input event with no key at all."""
    page = session.open("/")
    before = rows(page).count()
    page.evaluate("""() => { const box = document.getElementById('search');
        box.focus(); box.value = 'Binance';
        box.dispatchEvent(new InputEvent('input',
            {bubbles: true, inputType: 'insertFromPaste', data: 'Binance'})); }""")
    settle(page)
    assert 0 < rows(page).count() < before, session.where("a paste with no key applied")


@viewports(DESKTOP)
def test_clearing_the_search_brings_everything_back(session, viewport):
    page = session.open("/")
    before = rows(page).count()
    page.locator("#search").press_sequentially("Binance", delay=20)
    settle(page)
    assert rows(page).count() < before
    # The native clear (the × in a search box, or Escape in it): an input
    # event and a "search" event, no keyup of a printable key.
    page.evaluate("""() => { const box = document.getElementById('search');
        box.value = '';
        box.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContent'}));
        box.dispatchEvent(new Event('search', {bubbles: true})); }""")
    settle(page)
    assert rows(page).count() == before, session.where("cleared: everything is back")
    assert "q" not in query(page.url), session.where(page.url)


@viewports(DESKTOP)
def test_typing_sends_one_search_when_it_pauses(session, viewport):
    page = session.open("/")
    seen = record_requests(page)
    page.locator("#search").press_sequentially("Binance", delay=40)
    settle(page)
    searches = [p for p in seen if p.startswith("/activity") and "q=" in p]
    assert [query(p)["q"] for p in searches] == ["Binance"], session.where(f"{searches}")


@viewports(DESKTOP)
def test_a_slow_answer_to_an_old_search_is_never_shown(session, viewport):
    """The first search is answered after the second: its rows must not
    replace the second's."""
    page = session.open("/")
    held = []

    def hold(route):
        if query(route.request.url).get("q") == "USDC":
            held.append(route)
        else:
            route.continue_()

    page.route("**/activity?*", hold)
    box = page.locator("#search")
    box.fill("USDC")
    page.wait_for_function("() => true")
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held, session.where("the first search was sent")

    box.fill("Binance")
    settle(page, 500)
    for route in held:      # the old answer arrives last
        try:
            route.continue_()
        except Exception:   # aborted by the page: exactly what should happen
            pass
    settle(page, 500)
    page.unroute("**/activity?*")
    counterparties = page.locator(f"{ROWS} .cell-party").all_inner_texts()
    assert counterparties and all("Binance" in c for c in counterparties), \
        session.where(f"rows answer 'Binance': {counterparties[:5]}")
    assert box.input_value() == "Binance" and query(page.url).get("q") == "Binance"


# ------------------------------------------------ S7.1: what search matches


def search_for(page, text):
    box = page.locator("#search")
    box.fill("")
    box.press_sequentially(text, delay=15)
    settle(page)


@viewports(DESKTOP)
def test_search_finds_a_built_in_name_within_the_chosen_wallet(session, viewport):
    page = session.open("/")
    page.locator(".wallet__hit", has_text="Trading").click()
    settle(page)
    search_for(page, "universal router")
    hashes = page.locator(ROWS).evaluate_all("rows => rows.map(r => r.dataset.tx)")
    assert hashes == [tx_hash("ui-to-router")], session.where(f"in Trading: {hashes}")
    assert "Uniswap" in page.locator(ROWS).first.inner_text()

    page.locator(".wallet__hit", has_text="Cold").click()
    settle(page)
    assert rows(page).count() == 0, session.where("Cold never dealt with the router")

    page.locator(".wallets__group--all").click()
    settle(page)
    assert rows(page).count() == 1, session.where("all activity: the one row")
    assert query(page.url) == {"q": "universal router"}, session.where(page.url)


@pytest.mark.parametrize("text", ["%", "_", "\\", "' OR 1=1 --"])
@viewports(DESKTOP)
def test_search_text_is_never_a_pattern(session, viewport, text):
    page = session.open("/")
    search_for(page, text)
    assert rows(page).count() == 0, session.where(f"{text!r} matches no name in the stand")
    assert "No activity matches" in page.locator("#feed-body").inner_text()
    search_for(page, "")
    assert rows(page).count() >= 20


# ------------------------------------------------- U-08: one request path


@viewports(DESKTOP)
def test_the_hidden_note_follows_the_sidebar(session, viewport):
    page = session.open("/")
    seen = record_requests(page)
    whale = session.stand.account_id("Whale #1")
    page.locator(".wallet__hit", has_text="Whale #1").click()
    settle(page)
    notes = [p for p in seen if p.startswith("/dust-note")]
    assert any(query(p).get("wallet") == str(whale) for p in notes), \
        session.where(f"the note asked again for the wallet: {notes}")
    assert query(page.url).get("wallet") == str(whale), session.where(page.url)


@viewports(DESKTOP)
def test_switching_quickly_ends_on_the_last_choice(session, viewport):
    page = session.open("/")
    for name in ("Main", "Cold", "Trading", "Cold"):
        page.locator(".wallet__hit", has_text=name).click()
    settle(page)
    cold = session.stand.account_id("Cold")
    assert query(page.url).get("wallet") == str(cold)
    wallets = {w.strip() for w in page.locator(f"{ROWS} .cell-wallet").all_inner_texts()}
    assert page.locator(".wallet.is-selected .wallet__name").inner_text() == "Cold"
    assert rows(page).count() >= 1 and wallets <= {"", "Cold"} | {
        w for w in wallets if "Cold" in w}, session.where(f"rows of {wallets}")


# --------------------------------------------- U-12: pages loaded by scrolling


def scroll_to(page, y):
    page.evaluate("""(y) => { window.scrollTo(0, y);
        const box = document.querySelector('.table-scroll');
        if (box) box.scrollTo(0, y); }""", y)


@viewports(DESKTOP)
def test_scrolled_pages_survive_the_status_poll(session, viewport):
    page = session.open("/?dust=show")
    first = rows(page).count()
    scroll_to(page, 1e6)
    page.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length > {first}",
                           timeout=5000)
    loaded = rows(page).count()
    scroll_to(page, 0)
    # Two status polls (every 5 s) and one feed refresh tick (every 15 s).
    page.wait_for_timeout(16_500)
    assert rows(page).count() >= loaded, session.where(
        f"{loaded} rows loaded, {rows(page).count()} left after the polls")


# --------------------------------------------------- wallet change events


def add_removable(session):
    stand = session.stand
    response = session.page.request.post(stand.url + "/wallets", form={
        "name": "Removable", "address": REMOVABLE, "chain": "ethereum", "owned": "on"},
        headers=SCRIPT)
    assert response.ok
    session.page.request.post(stand.url + "/__stand__/sync", headers=SCRIPT)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if stand.db.get_sync_status("ethereum").activity_count > 100:
            break
        time.sleep(0.1)
    return stand.account_id("Removable")


@viewports(DESKTOP)
def test_removing_the_selected_wallet_never_asks_for_it_again(fresh_session, viewport):
    session = fresh_session
    removable = add_removable(session)
    page = session.open(f"/?wallet={removable}")
    assert rows(page).count() >= 1
    seen = record_requests(page)

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('summary[aria-label="Actions for Removable"]').click()
    page.locator(".wallet__menu[open] .menu__danger").click()
    page.wait_for_function("() => !document.querySelector('#wallet-value').value", timeout=5000)
    settle(page, 1200)

    after = seen
    stale = [p for p in after if query(p).get("wallet") == str(removable)]
    assert not stale, session.where(f"asked about the removed wallet: {stale}")
    for path in ("/activity", "/wallets", "/summary", "/dust-note"):
        sent = [p for p in after if urlparse(p).path == path]
        assert len(sent) == 1, session.where(f"{path} sent {len(sent)} times: {after}")
    assert "wallet" not in query(page.url), session.where(page.url)
    assert rows(page).count() >= 20 and "Removable" not in page.locator("#feed-body").inner_text()


@viewports(DESKTOP)
def test_adding_a_wallet_updates_the_page_once(fresh_session, viewport):
    session = fresh_session
    page = session.open("/")
    seen = record_requests(page)
    page.locator(".add-wallet-link").click()
    page.wait_for_selector("#wallet-dialog[open] input[name=address]")
    page.fill("#wallet-dialog input[name=name]", "Removable")
    page.fill("#wallet-dialog input[name=address]", REMOVABLE)
    page.keyboard.press("Enter")
    page.wait_for_function("() => !document.querySelector('#wallet-dialog').open", timeout=5000)
    settle(page, 1200)
    assert page.locator(".wallet__name", has_text="Removable").count() == 1
    after = [p for p in seen if not p.startswith("/wallets/form")]
    for path in ("/wallets", "/summary"):
        sent = [p for p in after if urlparse(p).path == path]
        assert len(sent) == 1, session.where(f"{path} sent {len(sent)} times: {after}")
    focused = page.evaluate("() => document.activeElement.className")
    assert "add-wallet-link" in focused, session.where(f"focus back on the opener: {focused}")


# ------------------------------------------ a link to a removed wallet


@viewports(DESKTOP, MOBILE)
def test_a_link_to_a_removed_wallet_says_so(fresh_session, viewport):
    session = fresh_session
    removable = add_removable(session)
    session.page.request.post(f"{session.stand.url}/wallets/{removable}/remove",
                              headers=SCRIPT)
    page = session.open(f"/?wallet={removable}")

    notice = page.locator(".scope-missing")
    assert notice.is_visible(), session.where("the feed says the wallet is gone")
    assert "Removable" in notice.inner_text() and "no longer watched" in notice.inner_text()
    assert rows(page).count() == 0, session.where("none of its activity")
    if viewport.mobile:
        label = page.locator(".topline__scope").inner_text()
        assert "All activity" not in label, session.where(f"scope reads {label!r}")
    else:
        assert page.locator(".wallets__group--all.is-selected").count() == 0

    notice.get_by_role("link", name="Show all activity").click()
    settle(page)
    assert page.locator(".scope-missing").count() == 0
    assert rows(page).count() >= 20, session.where("back to everything")
    assert "wallet" not in query(page.url), session.where(page.url)
    assert not run_axe(page)


@viewports(DESKTOP)
def test_back_and_forward_through_a_removed_wallet(fresh_session, viewport):
    session = fresh_session
    removable = add_removable(session)
    session.page.request.post(f"{session.stand.url}/wallets/{removable}/remove",
                              headers=SCRIPT)
    page = session.open("/")
    session.open(f"/?wallet={removable}")
    assert page.locator(".scope-missing").is_visible()
    page.go_back()
    settle(page)
    assert page.locator(".scope-missing").count() == 0 and rows(page).count() >= 20, \
        session.where("back: all activity")
    page.go_forward()
    settle(page)
    assert page.locator(".scope-missing").is_visible(), session.where("forward: the notice again")
    assert rows(page).count() == 0


# ------------------------------------------- the inspector during a refresh


@viewports(DESKTOP, MOBILE)
def test_a_feed_refresh_leaves_the_open_inspector_alone(session, viewport):
    page = session.open("/")
    row = rows(page).nth(2)
    activity = row.get_attribute("data-activity")
    row.locator(".row__open").focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.classList.contains('inspector-open')")
    page.wait_for_function("() => !!document.activeElement.closest('#inspector')")

    with page.expect_response(lambda r: "/activity" in r.url):
        page.evaluate("() => document.body.dispatchEvent(new Event('refreshFeed'))")
    settle(page, 300)
    assert page.evaluate("() => document.documentElement.classList.contains('inspector-open')")
    assert page.evaluate("() => !!document.activeElement.closest('#inspector')"), \
        session.where("focus stayed in the inspector")
    assert page.locator(f'{ROWS}[data-activity="{activity}"].is-open').count() == 1, \
        session.where("the open row is still marked")

    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.documentElement.classList.contains('inspector-open')")
    focused = page.evaluate(
        "() => document.activeElement.closest('tr[data-activity]')?.dataset.activity")
    assert focused == activity, session.where(f"focus back on the row: {focused}")


# ---------------------------------------------------------------- skip link


@viewports(DESKTOP, TABLET, MOBILE, SMALL)
def test_the_skip_link_reaches_the_feed_in_two_presses(session, viewport):
    page = session.open("/")
    page.keyboard.press("Tab")
    skip = page.locator(".skip-link")
    assert page.evaluate("() => document.activeElement.classList.contains('skip-link')"), \
        session.where("the first Tab stop is the skip link")
    box = skip.bounding_box()
    assert box and box["width"] > 20 and box["height"] > 10 and box["x"] >= 0 \
        and box["x"] + box["width"] <= viewport.width, session.where(f"visible on focus: {box}")
    page.keyboard.press("Enter")
    where = page.evaluate("() => document.activeElement.className")
    assert "row__open" in where, session.where(f"Enter lands on a feed row: {where}")
    after = skip.bounding_box()
    assert after is None or after["y"] + after["height"] <= 0, \
        session.where(f"hidden again once focus moves on: {after}")
    assert page.evaluate("() => document.documentElement.scrollWidth") <= viewport.width


@viewports(DESKTOP)
def test_the_skip_link_works_after_the_feed_is_replaced(session, viewport):
    page = session.open("/")
    page.locator(".wallet__hit", has_text="Cold").click()
    settle(page)
    page.evaluate("() => document.activeElement.blur()")
    page.locator(".skip-link").focus()
    page.keyboard.press("Enter")
    row = page.evaluate("() => document.activeElement.closest('tr[data-activity]') && "
                        "document.activeElement.className")
    assert row and "row__open" in row, session.where(f"after a swap: {row}")


@pytest.mark.parametrize("scheme", ["dark", "light"])
@viewports(DESKTOP, MOBILE)
def test_the_focused_skip_link_passes_axe(session, viewport, scheme):
    session.page.emulate_media(color_scheme=scheme)
    page = session.open("/")
    page.keyboard.press("Tab")
    found = run_axe(page)
    assert not found, session.where(f"{scheme}: {found}")
