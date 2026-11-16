"""Fixtures for the browser smoke tests.

Every page:

* runs against a local stand (tests_ui/stand.py), never the network — any
  request that is not for the stand is aborted and reported;
* collects JavaScript errors, console errors and CSP violations, and fails
  the test on any;
* records a Playwright trace, kept with a screenshot and the server log when
  the test fails, under $BLOCKTAIL_UI_ARTIFACTS (default tests_ui/artifacts).

Failures name the viewport and the scenario in the test id, e.g.
``test_inspector_opens_and_closes[mobile-390]``.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

try:
    from playwright import sync_api as playwright_api
except ImportError:  # the main suite's environment: skip, do not break collection
    playwright_api = None

from stand import Stand

HERE = Path(__file__).resolve().parent
ARTIFACTS = Path(os.environ.get("BLOCKTAIL_UI_ARTIFACTS", HERE / "artifacts"))
AXE = HERE / "vendor" / "axe-core-4.10.2" / "axe.min.js"

# What a script — not the page — sends with a write: it has no fetch metadata
# to prove where it came from, so it says it is a client of this app the way
# curl would (see app/security.py).
SCRIPT = {"HX-Request": "true"}

# Every CSP violation in any document of the context, reported as an error:
# init scripts and bindings run outside the page's policy.
CSP_LISTENER = """
document.addEventListener('securitypolicyviolation', (e) => {
  window.__blocktailCsp(e.effectiveDirective + ' refused ' + (e.blockedURI || 'inline')
    + ' (' + (e.sourceFile || 'page') + ':' + e.lineNumber + ')');
});
"""

# Every answer the feed has shown, as the query string it answered. A response
# htmx was told not to swap (an answer to a question no longer asked) is not
# here: afterSwap fires only for what was swapped in.
FEED_LOG = """
window.__feedAnswers = [];
document.addEventListener('htmx:afterSwap', (e) => {
  const target = e.detail.target;
  if (target && target.id === 'feed-body' && e.detail.xhr) {
    window.__feedAnswers.push(new URL(e.detail.xhr.responseURL).search);
  }
});
"""

# Nothing htmx sent is still out, and nothing it swapped is still settling.
HTMX_IDLE = "!document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling')"

# The feed shows the answer to the question the filter form asks now — the
# same filters, read the way the page compares them (what is empty or "all"
# is no filter; a page cursor is not part of the question) — swapped in after
# the first `since` answers, and htmx is idle: whatever followed the answer
# (the hidden-transfers note, the sidebar, the summary) has arrived too.
FEED_ANSWERS_FORM = """(since) => {
  const question = (search) => [...new URLSearchParams(search)]
    .filter(([k, v]) => v && k !== 'before' && k !== 'after_day'
            && !((k === 'scope' || k === 'direction') && v === 'all'))
    .map(([k, v]) => k + '=' + v).sort().join('&');
  const answers = window.__feedAnswers || [];
  if (answers.length <= since) return false;
  const asked = new URLSearchParams(new FormData(document.getElementById('filters')));
  return question(answers[answers.length - 1]) === question(asked.toString())
    && """ + HTMX_IDLE + """;
}"""


def feed_answers(page, since: int = 0) -> None:
    """Wait until the feed answers what the form asks (see FEED_ANSWERS_FORM).

    What a test waits for after it changes the feed, instead of a fixed time:
    on a slow server the answer simply comes later, and a test that read the
    page before it would see the previous answer."""
    page.wait_for_function(FEED_ANSWERS_FORM, arg=since)


def feed_answer_count(page) -> int:
    """How many answers the feed has shown so far; pass it to feed_answers
    to wait for a new answer to an unchanged question (after a wallet change)."""
    return page.evaluate("() => (window.__feedAnswers || []).length")


def htmx_idle(page) -> None:
    page.wait_for_function("() => " + HTMX_IDLE)


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int
    mobile: bool

    @property
    def id(self) -> str:
        return f"{self.name}-{self.width}"


DESKTOP = Viewport("desktop", 1440, 900, False)
TABLET = Viewport("tablet", 768, 1024, False)
MOBILE = Viewport("mobile", 390, 844, True)
SMALL = Viewport("small", 320, 640, True)
ALL_VIEWPORTS = [DESKTOP, TABLET, MOBILE, SMALL]


def viewports(*chosen: Viewport):
    return pytest.mark.parametrize("viewport", chosen, ids=[v.id for v in chosen])


@pytest.fixture(scope="session")
def browser():
    if playwright_api is None:
        pytest.skip("Playwright is not installed: pip install -r requirements-ui.txt"
                    " && python -m playwright install --only-shell chromium")
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


def _new_stand(tmp_path_factory, name: str) -> Stand:
    return Stand(tmp_path_factory.mktemp(name)).start()


@pytest.fixture(scope="session")
def stand(tmp_path_factory):
    """Shared by the tests that only read. Tests that change data (remove a
    wallet, request history) take a fresh_stand of their own."""
    s = _new_stand(tmp_path_factory, "stand")
    yield s
    s.stop()


@pytest.fixture
def fresh_stand(tmp_path_factory):
    s = _new_stand(tmp_path_factory, "fresh")
    yield s
    s.stop()


@dataclass
class Session:
    """A page at one viewport, and what it saw."""

    page: object
    context: object
    stand: Stand
    viewport: Viewport
    errors: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    # Console errors a test causes on purpose (taking the server down makes
    # the browser log failed requests). Each is a regex, declared by the test.
    expected: list[re.Pattern] = field(default_factory=list)

    def expect_console_errors(self, *patterns: str) -> None:
        self.expected.extend(re.compile(p) for p in patterns)

    def unexpected_errors(self) -> list[str]:
        return [e for e in self.errors if not any(p.search(e) for p in self.expected)]

    def open(self, path: str = "/") -> object:
        self.page.goto(self.stand.url + path)
        self.page.wait_for_load_state("networkidle")
        return self.page

    def where(self, scenario: str) -> str:
        """Context for an assertion message."""
        return f"[{self.viewport.id}] {scenario}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def _start_session(browser, request, stand: Stand, viewport: Viewport) -> Session:
    context = browser.new_context(
        viewport={"width": viewport.width, "height": viewport.height},
        is_mobile=viewport.mobile,
        has_touch=viewport.mobile,
        device_scale_factor=1,
        timezone_id="UTC",
        locale="en-US",
    )
    context.tracing.start(screenshots=True, snapshots=True)
    errors: list[str] = []
    context.expose_binding("__blocktailCsp", lambda source, text: errors.append(f"csp: {text}"))
    context.add_init_script(CSP_LISTENER)
    context.add_init_script(FEED_LOG)
    page = context.new_page()
    # BLOCKTAIL_UI_CPU_SLOWDOWN=4 runs the page on a quarter of the CPU, as on
    # a slow CI runner: a test that only passes on a fast machine shows here.
    slowdown = float(os.environ.get("BLOCKTAIL_UI_CPU_SLOWDOWN") or 1)
    if slowdown > 1:
        context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": slowdown})
    session = Session(page=page, context=context, stand=stand, viewport=viewport, errors=errors)

    # BLOCKTAIL_UI_LATENCY_MS: see stand._with_latency (the server waits,
    # not this handler, which would serialise every request).

    def offline(route):
        if route.request.url.startswith(stand.url):
            route.continue_()
        else:
            session.blocked.append(route.request.url)
            route.abort()

    context.route("**/*", offline)
    page.on("pageerror", lambda exc: session.errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: session.errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return session


def _finish_session(request, session: Session) -> None:
    failed = getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed
    if failed:
        safe = re.sub(r"[^\w.-]+", "_", request.node.nodeid)
        target = ARTIFACTS / safe
        target.mkdir(parents=True, exist_ok=True)
        try:
            session.page.screenshot(path=str(target / "page.png"), full_page=True)
        except playwright_api.Error:  # the page may be the thing that broke
            pass
        session.context.tracing.stop(path=str(target / "trace.zip"))
        if session.stand.log_path.exists():
            shutil.copy(session.stand.log_path, target / "server.log")
        (target / "console.txt").write_text("\n".join(session.errors) or "(none)")
    else:
        session.context.tracing.stop()
    session.context.close()


@pytest.fixture
def session(browser, request, stand, viewport):
    s = _start_session(browser, request, stand, viewport)
    yield s
    _finish_session(request, s)
    assert not s.blocked, f"{s.where('offline')}: requests left the stand: {s.blocked}"
    unexpected = s.unexpected_errors()
    assert not unexpected, f"{s.where('console')}: JavaScript errors: {unexpected}"


@pytest.fixture
def fresh_session(browser, request, fresh_stand, viewport):
    s = _start_session(browser, request, fresh_stand, viewport)
    yield s
    _finish_session(request, s)
    assert not s.blocked, f"{s.where('offline')}: requests left the stand: {s.blocked}"
    unexpected = s.unexpected_errors()
    assert not unexpected, f"{s.where('console')}: JavaScript errors: {unexpected}"


def run_axe(page) -> dict[str, list[str]]:
    """axe-core violations on the current page: rule id -> impacts of its nodes."""
    # Evaluated, not added as a <script>: the page's CSP refuses inline script,
    # and rightly — the test tool is not an exception the app should make.
    page.evaluate(AXE.read_text(encoding="utf-8"))
    result = page.evaluate(
        "async () => (await axe.run(document, {resultTypes: ['violations']})).violations"
        ".map(v => [v.id, v.impact, v.nodes.length])"
    )
    return {rule: [impact, count] for rule, impact, count in result}
