"""The page under its Content-Security-Policy (S8a).

The policy allows script and style from this origin only — no inline script,
no inline handler, no eval. These hold the page to it: the policy is served
and enforced, and every control that used to carry an inline handler still
does its job. Any CSP violation in any test fails it (see conftest), so the
whole suite is the wider smoke under the policy.
"""

from __future__ import annotations

import pytest
from conftest import CSP_LISTENER, DESKTOP, MOBILE, viewports
from stand import REMOVABLE, Stand

ROWS = "#feed-body tr[data-activity]"
POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def settle(page, ms: int = 600):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(ms)


@viewports(DESKTOP)
def test_the_page_is_served_under_the_policy(session, viewport):
    response = session.page.goto(session.stand.url + "/")
    headers = response.headers
    assert headers["content-security-policy"] == POLICY
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["cache-control"] == "no-store"


@viewports(DESKTOP)
def test_the_policy_is_enforced_not_just_sent(session, viewport):
    """Script the page did not ship is refused — the point of the policy."""
    page = session.open("/")
    session.expect_console_errors(r"Content Security Policy", r"^csp: ")
    page.evaluate("""() => {
        const s = document.createElement('script');
        s.textContent = 'window.__injected = 1';
        document.body.appendChild(s);
        const b = document.createElement('button');
        b.setAttribute('onclick', 'window.__clicked = 1');
        document.body.appendChild(b);
        b.click();
    }""")
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.__injected") is None
    assert page.evaluate("() => window.__clicked") is None
    refused = [e for e in session.errors if e.startswith("csp: ")]
    assert any("script-src" in e for e in refused), refused
    assert page.evaluate("() => typeof htmx === 'object' && htmx.config.allowEval") is False


@viewports(DESKTOP)
def test_every_control_works_under_the_policy_on_a_wide_screen(fresh_session, viewport):
    session = fresh_session
    page = session.open("/")

    # Filters: a radio and the select ask for the feed again.
    page.locator(".segment--in").click()
    page.wait_for_function("() => location.search.includes('direction=in')")
    page.locator(".segment--all").click()
    page.wait_for_function("() => !location.search.includes('direction')")
    page.select_option("select[name=asset]", label="USDC")
    page.wait_for_function("() => location.search.includes('asset=')")
    page.select_option("select[name=asset]", value="")
    settle(page)

    # A row opens the inspector; its close button closes it.
    page.locator(ROWS).first.locator(".cell-party").click()
    page.wait_for_selector("#inspector .insp")
    assert page.evaluate("() => document.documentElement.classList.contains('inspecting')"
                         " || !document.getElementById('inspector').inert")
    page.locator("#inspector .insp__close").click()
    page.wait_for_function("() => document.getElementById('inspector').inert")

    # The fold-out shows the rest of a transaction.
    fold = page.locator(".foldout").first
    fold.click()
    assert fold.get_attribute("aria-expanded") == "true"
    fold.click()
    assert fold.get_attribute("aria-expanded") == "false"

    # The hidden-transfers note: Show, then Hide.
    page.locator("#dust-note button").click()
    page.wait_for_function("() => location.search.includes('dust=show')")
    settle(page)
    page.locator("#dust-note button").click()
    page.wait_for_function("() => !location.search.includes('dust=show')")
    settle(page)

    # The holdings panel folds and unfolds.
    page.locator(".summary__toggle").click()
    assert page.evaluate("() => document.documentElement.classList.contains('summary-collapsed')")
    page.locator(".summary__toggle").click()
    assert not page.evaluate(
        "() => document.documentElement.classList.contains('summary-collapsed')")

    # Hidden tokens: shown, then hidden again.
    hidden = page.locator(".figure__note .linkish")
    if hidden.count():
        hidden.click()
        assert page.evaluate(
            "() => document.documentElement.classList.contains('show-hidden-tokens')")
        hidden.click()

    # All holdings opens in the dialog; its close button closes it.
    page.locator(".holdings__more").click()
    page.wait_for_selector("#wallet-dialog[open] .insp__close")
    page.locator("#wallet-dialog .insp__close").click()
    page.wait_for_function("() => !document.getElementById('wallet-dialog').open")

    # Add a wallet: open, cancel, open, submit (an htmx POST).
    page.locator(".add-wallet-link").click()
    page.wait_for_selector("#wallet-dialog[open] .sheet__cancel")
    page.locator("#wallet-dialog .sheet__cancel").click()
    page.wait_for_function("() => !document.getElementById('wallet-dialog').open")
    page.locator(".add-wallet-link").click()
    page.wait_for_selector("#wallet-dialog[open] input[name=address]")
    page.fill("#wallet-dialog input[name=name]", "Removable")
    page.fill("#wallet-dialog input[name=address]", REMOVABLE)
    page.keyboard.press("Enter")
    page.wait_for_function("() => !document.getElementById('wallet-dialog').open")
    settle(page)
    assert page.locator(".wallet__name", has_text="Removable").count() == 1

    # Its menu: copy the address, rename it, then remove it.
    session.context.grant_permissions(["clipboard-read", "clipboard-write"],
                                      origin=session.stand.url)
    menu = 'summary[aria-label="Actions for Removable"]'
    page.locator(menu).click()
    page.locator(".wallet__menu[open] button", has_text="Copy address").click()
    assert page.evaluate("() => navigator.clipboard.readText()").lower() == REMOVABLE.lower()
    page.locator(menu).click()  # closes
    page.locator(menu).click()
    page.locator(".wallet__menu[open] button", has_text="Rename").click()
    page.wait_for_selector("#wallet-dialog[open] input[name=name]")
    page.fill("#wallet-dialog input[name=name]", "Renamed")
    page.keyboard.press("Enter")
    page.wait_for_function("() => !document.getElementById('wallet-dialog').open")
    settle(page)
    assert page.locator(".wallet__name", has_text="Renamed").count() == 1
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('summary[aria-label="Actions for Renamed"]').click()
    page.locator(".wallet__menu[open] .menu__danger").click()
    settle(page, 1000)
    assert page.locator(".wallet__name", has_text="Renamed").count() == 0

    # A wallet chosen in the sidebar; its address copies from the summary.
    page.locator(".wallet__hit").first.click()
    page.wait_for_function("() => location.search.includes('wallet=')")
    settle(page)
    page.locator(".summary__addr").click()
    page.wait_for_function("() => document.querySelector('.summary__addr.is-copied')")


@viewports(DESKTOP)
def test_older_history_is_requested_under_the_policy(fresh_session, viewport):
    """A form the page posts through htmx, end to end."""
    session = fresh_session
    # One wallet's feed is short enough to end on the page.
    page = session.open(f"/?wallet={session.stand.account_id('Main')}")
    page.locator("#history-row button[type=submit]").click()
    page.wait_for_selector("#history-row.is-busy", timeout=3000)


@viewports(MOBILE)
def test_every_control_works_under_the_policy_on_a_phone(session, viewport):
    page = session.open("/")

    # Search is a mode: open it, type, close it (which clears it).
    page.locator(".field-toggle--open").click()
    assert page.evaluate("() => document.activeElement.id") == "search"
    page.keyboard.type("Binance")
    page.wait_for_function("() => location.search.includes('q=Binance')")
    page.locator(".field-toggle--close").click()
    page.wait_for_function("() => !location.search.includes('q=')")
    settle(page)

    # The scope sheet: opened from the status bar, closed by the scrim.
    page.locator(".topline__scope").click()
    page.wait_for_function("() => document.documentElement.classList.contains('scope-open')")
    page.locator(".scrim").click(position={"x": 5, "y": 5})
    page.wait_for_function("() => !document.documentElement.classList.contains('scope-open')")

    # A row opens the inspector as a sheet; its close button closes it.
    page.locator(ROWS).first.locator(".row__open").click()
    page.wait_for_selector("#inspector .insp")
    page.locator("#inspector .insp__close").click()
    page.wait_for_function("() => document.getElementById('inspector').inert")


# ------------------------------------------------------------ basic auth


@pytest.fixture
def guarded_stand(tmp_path_factory):
    s = Stand(tmp_path_factory.mktemp("guarded"), auth_user="watcher",
              auth_password="correct-horse-battery").start()
    yield s
    s.stop()


def test_basic_auth_in_a_browser_under_the_policy(browser, guarded_stand):
    """The browser attaches the credentials to the page's htmx requests — the
    reason the origin check exists — and the page works under the policy."""
    stand = guarded_stand
    stranger = browser.new_context()
    response = stranger.request.get(stand.url + "/")
    assert response.status == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers["content-security-policy"] == POLICY
    assert response.headers["cache-control"] == "no-store"
    stranger.close()

    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, timezone_id="UTC",
        http_credentials={"username": "watcher", "password": "correct-horse-battery"})
    errors: list[str] = []
    context.expose_binding("__blocktailCsp", lambda source, text: errors.append(f"csp: {text}"))
    context.add_init_script(CSP_LISTENER)
    page = context.new_page()
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(stand.url + "/")
        page.wait_for_load_state("networkidle")
        page.locator(".add-wallet-link").click()
        page.wait_for_selector("#wallet-dialog[open] input[name=address]")
        page.fill("#wallet-dialog input[name=name]", "Removable")
        page.fill("#wallet-dialog input[name=address]", REMOVABLE)
        page.keyboard.press("Enter")
        page.wait_for_function("() => !document.getElementById('wallet-dialog').open")
        settle(page)
        assert page.locator(".wallet__name", has_text="Removable").count() == 1
        assert not errors, errors
    finally:
        context.close()
