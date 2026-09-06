"""HTTP layer: rendering, filtering, search, pagination, status, JSON API."""

from __future__ import annotations

import pathlib
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import NATIVE, TransferKind
from tests.conftest import (
    BINANCE,
    AssetRef,
    Balance,
    USDC,
    COLD,
    PAYMENTS,
    MAIN,
    STRANGER,
    make_config,
    run,
    transfer,
)


@pytest.fixture
def client(settings, watch_config, provider, prices, sample_transfers):
    provider.transfers = sample_transfers
    app = create_app(
        settings=settings, config=watch_config, provider=provider,
        price_source=prices, run_indexer=False,
    )
    with TestClient(app) as test_client:
        run(app.state.ctx.indexer.run_once())
        test_client.ctx = app.state.ctx
        yield test_client


@pytest.fixture
def busy_client(settings, watch_config, provider, prices):
    """A feed long enough to page through."""
    provider.head_block = 21_000_200
    provider.transfers = [
        transfer(
            f"bulk-{index}",
            sender=STRANGER if index % 2 else MAIN,
            recipient=MAIN if index % 2 else STRANGER,
            amount=(index + 1) * 10**15,
            block=21_000_000 + index,
        )
        for index in range(120)
    ]
    app = create_app(
        settings=settings, config=watch_config, provider=provider,
        price_source=prices, run_indexer=False,
    )
    with TestClient(app) as test_client:
        run(app.state.ctx.indexer.run_once())
        test_client.ctx = app.state.ctx
        yield test_client


def account_id(client, name: str) -> int:
    return next(a.id for a in client.ctx.db.list_accounts() if a.name == name)


def sentinel_params(body: str) -> dict[str, str] | None:
    """Query parameters of the infinite-scroll sentinel, if the page has one."""
    match = re.search(r'id="feed-more"\s+hx-get="/activity\?([^"]*)"', body)
    if not match:
        return None
    return dict(pair.split("=", 1) for pair in match.group(1).split("&"))


def wallet_blocks(body: str) -> list[str]:
    """The sidebar's wallet blocks, split without depending on nesting."""
    parts = re.split(r'(?=<div class="wallet(?:"| is-selected"))', body)
    return [part for part in parts if re.match(r'<div class="wallet(?:"| is-selected")', part)]


# ------------------------------------------------------------------ dashboard


def test_dashboard_renders_wallets_and_balances(client):
    body = client.get("/").text

    assert "blocktail" in body
    assert 'class="caret"' in body, "the branding carries its blinking caret"
    for name in ("Main", "Cold", "Payments"):
        assert f">{name}</span>" in body
    assert "36.423" in body, "the native total across my wallets"
    assert "USDC" in body


def test_dashboard_shows_the_activity_feed(client):
    body = client.get("/").text

    assert ">IN<" in body and ">OUT<" in body
    assert "Binance" in body, "a configured label must replace the raw address"
    assert "2.4" in body and "500" in body
    assert "https://etherscan.io/tx/0x" in body
    assert "https://etherscan.io/address/0x" in body
    assert re.search(r"0x[0-9a-fA-F]{4}…[0-9a-fA-F]{4}", body), "unknown addresses are shortened"


def test_wallet_to_wallet_renders_as_one_row(client):
    body = client.get("/").text

    assert re.search(r'<span class="pair">Main<span class="arrow">&rarr;</span>Cold</span>', body), (
        "a transfer between monitored wallets should read Main -> Cold"
    )


def test_own_transfers_carry_no_direction_badge(client):
    """`Main -> Cold` is its own kind of event; IN/OUT would only confuse it,
    and INT reads as 'internal transaction', which it is not."""
    row = re.search(r'<tr class="row row--between".*?</tr>', client.get("/").text, re.S).group(0)

    assert ">IN<" not in row and ">OUT<" not in row and ">INT<" not in row
    assert 'class="cell-dir"' not in row, "the direction column is spent on the wallet pair"
    assert 'colspan="2"' in row


def test_is_read_only_and_leaks_no_secrets(client):
    body = client.get("/").text.lower()

    for forbidden in ("private key", "seed phrase", "connect wallet", "walletconnect", "metamask"):
        assert forbidden not in body
    assert "alchemy" not in body
    assert client.ctx.settings.alchemy_url not in client.get("/").text
    assert client.get("/api/status").text.find("alchemy_url") == -1
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/").status_code in (404, 405)


# -------------------------------------------------------------------- filters


def test_filter_by_wallet(client):
    body = client.get("/activity", params={"wallet": account_id(client, "Payments")}).text
    assert "Payments" in body
    assert "0.05" in body
    assert "Binance" not in body, "Cold's USDC transfer must not appear under Payments"


def test_filter_by_direction(client):
    inbound = client.get("/activity", params={"direction": "in"}).text
    outbound = client.get("/activity", params={"direction": "out"}).text

    assert ">IN<" in inbound and ">OUT<" not in inbound
    assert ">OUT<" in outbound and ">IN<" not in outbound


def test_direction_combines_with_wallet(client):
    main = account_id(client, "Main")
    inbound = client.get("/activity", params={"wallet": main, "direction": "in"}).text
    outbound = client.get("/activity", params={"wallet": main, "direction": "out"}).text

    assert "2.4" in inbound
    assert "0.12" in outbound
    # Main -> Cold is outgoing for Main, and must not show up as incoming.
    assert "5</td>" not in inbound or "&rarr;" not in inbound


def test_filter_by_asset(client):
    usdc = next(row["id"] for row in client.ctx.db.feed_assets() if row["symbol"] == "USDC")
    body = client.get("/activity", params={"asset": usdc}).text

    assert "USDC" in body
    assert "Binance" in body
    assert body.count('data-activity=') == 1


def test_search_by_address_tx_hash_and_label(client):
    rows = client.ctx.db.query_activities(limit=50)
    some_hash = rows[0]["tx_hash"]

    by_label = client.get("/activity", params={"q": "binance"}).text
    by_address = client.get("/activity", params={"q": BINANCE[2:14]}).text
    by_hash = client.get("/activity", params={"q": some_hash}).text
    by_wallet_name = client.get("/activity", params={"q": "cold"}).text
    no_match = client.get("/activity", params={"q": "definitely-not-there"}).text

    assert "Binance" in by_label and by_label.count("data-activity=") == 1
    assert "Binance" in by_address
    assert some_hash[:10] in by_hash and by_hash.count("data-activity=") == 1
    assert "Cold" in by_wallet_name
    assert "No activity matches these filters" in no_match


def test_unparseable_filters_fall_back_to_defaults(client):
    body = client.get(
        "/activity", params={"wallet": "abc", "direction": "sideways", "asset": "xyz"}
    ).text
    assert body.count("<tr") >= 5


# ----------------------------------------------------------------- pagination


def test_pagination_walks_the_whole_feed_without_gaps_or_repeats(busy_client):
    seen: list[str] = []
    url = "/activity"
    params: dict = {}
    pages = 0

    while pages < 10:
        body = busy_client.get(url, params=params).text
        seen.extend(re.findall(r'class="hash"[^>]*title="(0x[0-9a-f]{64})', body))
        pages += 1
        next_params = sentinel_params(body)
        if next_params is None:
            break
        params = next_params

    assert pages > 1, "120 activities must not fit on one page"
    assert len(seen) == 120
    assert len(set(seen)) == 120, "no activity may be repeated across pages"


def test_page_size_is_bounded(busy_client):
    body = busy_client.get("/activity").text
    assert len(re.findall(r'class="row row--(?:in|out|between|self)"', body)) == 50


def test_last_page_has_no_sentinel(busy_client):
    rows = busy_client.ctx.db.query_activities(limit=200)
    oldest = rows[-1]
    body = busy_client.get(
        "/activity", params={"before": f"{oldest['block_timestamp']}_{oldest['id']}"}
    ).text
    assert sentinel_params(body) is None


# --------------------------------------------------------------------- status


def test_status_reports_a_healthy_sync(client):
    body = client.get("/status").text
    assert "status--ok" in body
    assert "Ethereum" in body
    assert "#21,000,100" in body
    assert re.search(r"\ds ago", body)
    assert "3 accounts" in body, "the top bar carries a one-line summary, not a dashboard"


def test_status_reports_provider_failure(client):
    client.ctx.db.record_failure("ethereum", "provider returned HTTP 429")
    body = client.get("/status").text

    assert "status--error" in body
    assert "429" in body


def test_status_reports_staleness_rather_than_pretending(client):
    stale = int(time.time()) - client.ctx.settings.stale_after - 60
    client.ctx.db._upsert_sync("ethereum", {"last_success_at": stale})
    body = client.get("/status").text

    assert "status--stale" in body
    assert "no successful sync" in body


def test_status_before_the_first_sync(settings, watch_config, provider):
    app = create_app(settings=settings, config=watch_config, provider=provider, run_indexer=False)
    with TestClient(app) as test_client:
        body = test_client.get("/status").text
        assert "status--starting" in body
        assert "initial index" in body
        assert "No activity indexed yet" in test_client.get("/").text


# ------------------------------------------------------------------ json api


def test_api_activity_is_chain_neutral(client):
    payload = client.get("/api/activity").json()
    activity = payload["activities"][0]

    assert activity["chain"] == "ethereum"
    assert set(activity) >= {"id", "chain", "kind", "direction", "asset", "amount", "from", "to"}
    assert not any("eth" in key.lower() for key in activity), activity.keys()


def test_api_accounts(client):
    accounts = client.get("/api/accounts").json()["accounts"]
    main = next(a for a in accounts if a["name"] == "Main")

    assert main["chain"] == "ethereum"
    assert main["address"].startswith("0x")
    assert main["balances"][0]["native"] is True
    assert main["balances"][0]["symbol"] == "ETH"
    assert main["balances"][0]["amount_raw"] == str(4_821_000_000_000_000_000)


def test_api_status_and_healthz(client):
    status = client.get("/api/status").json()
    assert status["chain"] == "ethereum"
    assert status["state"] == "ok"
    assert status["activity_count"] == 5

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_healthz_stays_up_while_the_provider_is_down(client):
    client.ctx.db.record_failure("ethereum", "upstream down")
    health = client.get("/healthz")

    assert health.status_code == 200, "a provider outage must not restart the container"
    assert health.json()["sync_state"] == "error"


def test_static_assets_are_served(client):
    for path in ("/static/app.css", "/static/htmx.min.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert len(response.content) > 500


# ------------------------------------------------------------ day grouping


@pytest.fixture
def dated_client(settings, watch_config, provider, prices):
    """Activity spread over today, yesterday and a week ago.

    Anchored to the local day boundary rather than to "an hour ago", which is
    yesterday for the two hours after midnight — a test that fails between 00:00
    and 02:00 in whatever timezone it happens to run in.
    """
    import datetime as dt

    now = int(time.time())
    day = 86_400
    midnight = int(
        dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    # Halfway between midnight and now: always today, and always in the past.
    early_today = midnight + (now - midnight) // 3
    late_today = midnight + 2 * (now - midnight) // 3

    provider.transfers = [
        transfer("today-1", sender=STRANGER, recipient=MAIN, amount=10**18,
                 block=21_000_090, timestamp=late_today),
        transfer("today-2", sender=MAIN, recipient=STRANGER, amount=2 * 10**17,
                 block=21_000_080, timestamp=early_today),
        transfer("yesterday", sender=STRANGER, recipient=COLD, amount=3 * 10**18,
                 block=21_000_070, timestamp=midnight - 3600),
        transfer("older", sender=PAYMENTS, recipient=STRANGER, amount=4 * 10**17,
                 block=21_000_060, timestamp=midnight - 7 * day),
    ]
    app = create_app(settings=settings, config=watch_config, provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as test_client:
        run(app.state.ctx.indexer.run_once())
        test_client.ctx = app.state.ctx
        yield test_client


def test_days_are_labelled_in_words_then_dates(dated_client):
    headings = re.findall(r'<span class="day">([^<]+)</span>', dated_client.get("/").text)

    assert headings[0] == "Today"
    assert headings[1] == "Yesterday"
    assert len(headings) == 3
    assert not re.match(r"^\d{4}-", headings[2]), "older days read as 'Sep 1', not an ISO date"


def test_every_row_sits_under_a_day_heading(dated_client):
    body = dated_client.get("/").text
    assert body.index('class="day"') < body.index("data-activity=")


def test_a_day_heading_never_repeats_across_pages(busy_client):
    """The sentinel hands the last rendered day to the next page, so a day that
    spans a page boundary is announced once, not once per page."""
    first = busy_client.get("/activity").text
    params = sentinel_params(first)

    assert params is not None and "after_day" in params
    assert first.count('<span class="day">') == 1, "the whole fixture is one day"

    second = busy_client.get("/activity", params=params).text
    assert second.count("data-activity=") > 0, "the second page has rows"
    assert second.count('<span class="day">') == 0, "the day already has its heading"

    # Without the hint the page would open with a heading of its own.
    del params["after_day"]
    assert busy_client.get("/activity", params=params).text.count('<span class="day">') == 1


# ------------------------------------------------------- visual noise budget


def test_only_internal_transfers_are_badged(client):
    feed = client.get("/activity").text
    badged = [row for row in rows_of(feed) if 'class="kind"' in row]

    assert len(badged) == 1, "native and token kinds are inferable and must not be labelled"
    assert ">int<" in badged[0]
    assert ">native<" not in feed and ">token<" not in feed


def test_amount_and_asset_are_one_cell(client):
    cell = re.search(r'<td class="cell-value">.*?</td>', client.get("/").text, re.S).group(0)
    assert 'class="amount"' in cell and 'class="sym"' in cell


# ------------------------------------------------------------ wallet select


def test_the_owned_accounts_need_no_heading_of_their_own(client):
    """The summary panel above already says whose wallets these are; repeating
    it directly underneath is the same word twice. "All activity" is not that
    repetition — it is the way back, and with nothing watched it is the only
    heading the list needs."""
    body = client.get("/wallets").text
    headings = re.findall(r'class="wallets__group[^"]*"[^>]*>\s*<span>([^<]+)</span>', body)

    assert "scope-link" not in body, "the redundant list of scopes is gone"
    assert [h.strip() for h in headings] == ["All activity"]
    assert body.index(">Main</span>") < body.index(">Cold</span>")


def test_returning_to_everything_is_a_place_not_an_undo(client):
    """Clicking the selected group again to clear it is discoverable only once
    you have already found it. On a phone this list is the only scope control
    there is, so getting back has to be somewhere you can go."""
    body = client.get("/wallets").text

    assert 'wallets__group--all' in body
    assert "All activity" in body
    assert body.index("All activity") < body.index(">Main</span>")


def test_the_two_groups_contrast_without_echoing_the_summary(mixed_client):
    """Both groups need a heading once there is something to contrast — and each
    heading is also its scope control. "My wallets" is not reused: it sits
    directly under the summary panel's own "My wallets"."""
    body = mixed_client.get("/wallets").text
    headings = re.findall(r'class="wallets__group[^"]*"[^>]*>\s*<span>([^<]+)</span>', body)

    assert headings == ["All activity", "My wallets", "Watching"]
    assert 'hx-get="/activity?scope=mine"' in body
    assert 'hx-get="/activity?scope=watched"' in body
    assert body.index("My wallets") < body.index(">Main</span>") < body.index("Watching")


def test_a_selected_group_offers_the_way_back(mixed_client):
    """Narrowing must be reversible without hunting: the active heading turns
    into its own clear control."""
    plain = mixed_client.get("/wallets").text
    assert "wallets__clear" not in plain
    assert "wallets__count" not in plain, "the count is already in the summary above"

    narrowed = mixed_client.get("/wallets", params={"scope": "watched"}).text
    active = re.search(r'<a class="wallets__group is-selected".*?</a>', narrowed, re.S).group(0)
    assert "wallets__clear" in active
    assert 'href="?"' in active and 'hx-get="/activity"' in active, "back to everything"


def test_sidebar_highlights_the_selected_wallet(client):
    cold = account_id(client, "Cold")
    body = client.get("/wallets", params={"wallet": cold}).text

    selected = [block for block in wallet_blocks(body) if block.startswith('<div class="wallet is-selected"')]
    assert len(selected) == 1
    assert ">Cold</span>" in selected[0]
    assert body.count("is-selected") == 1, "only the wallet itself is highlighted"


def test_the_sidebar_is_navigation_not_a_dashboard(client):
    """Name and a sense of scale. Addresses, per-asset balances and token lists
    belong to the summary panel, which opens for whichever wallet is selected."""
    body = client.get("/wallets").text

    assert 'class="wallet__name"' in body and 'class="wallet__value' in body
    for gone in ("wallet__addr", "wallet__native", 'class="tokens"', "4.821"):
        assert gone not in body, gone


# ------------------------------------------------- layout and click targets


def test_the_feed_spends_its_width_on_the_columns(client):
    """The columns themselves take the width. A filler column would leave the
    data huddled at the left of a wide display."""
    body = client.get("/").text
    assert 'class="cell-fill"' in body, "a spacer keeps rows off the container edge"
    assert "cell-block" not in body, "the block number lives in the row detail"


def test_the_header_names_every_column_it_has(client):
    header = re.search(r"<thead>.*?</thead>", client.get("/").text, re.S).group(0)
    labels = re.findall(r"<th[^>]*>(?:<span[^>]*>)?([A-Za-z]*)", header)
    assert [label for label in labels if label] == [
        "Time", "Wallet", "Dir", "Amount", "Counterparty", "Transaction",
    ]


def test_the_wallet_filter_has_no_second_control(client):
    """The sidebar is the wallet selector; a dropdown would be the same control
    twice. Only the hidden value that carries the choice into the form remains."""
    body = client.get("/").text
    assert '<select name="wallet"' not in body
    assert '<input type="hidden" name="wallet" id="wallet-value"' in body
    assert '<input type="hidden" name="scope" id="scope-value"' in body


def test_the_wallet_row_is_one_link_plus_a_menu(client):
    wallet = wallet_blocks(client.get("/wallets").text)[0]
    assert 'class="wallet__hit"' in wallet
    assert 'class="wallet__menu"' in wallet
    assert "Copy address" in wallet


def test_counterparty_filters_the_feed_and_offers_the_explorer_separately(client):
    row = re.search(r'<tr class="row row--in".*?</tr>', client.get("/").text, re.S).group(0)
    cell = re.search(r'<td class="cell-party">.*?</td>', row, re.S).group(0)

    assert 'hx-get="/activity?q=0x' in cell, "the name filters the feed"
    assert 'filterByAddress(' in cell
    assert 'class="ext"' in cell and "etherscan.io/address/" in cell, "the explorer is a separate icon"


def test_filtering_by_a_counterparty_address_finds_both_directions(client):
    """The workflow the click implements: every dealing any wallet has had with
    one address, inbound and outbound."""
    body = client.get("/activity", params={"q": STRANGER}).text
    assert ">IN<" in body and ">OUT<" in body
    assert body.count("data-activity=") == 3


def test_own_transfers_leave_the_counterparty_cell_empty(client):
    row = re.search(r'<tr class="row row--between".*?</tr>', client.get("/").text, re.S).group(0)
    party = re.search(r'<td class="cell-party">(.*?)</td>', row, re.S).group(1)
    assert party.strip() == "", "an own transfer has no counterparty to name"


# ------------------------------------------------------------------ holdings


def test_summary_answers_how_much_do_i_have(client):
    body = client.get("/summary").text

    assert "My wallets" in body
    assert "Total value" in body and "Native" in body and "Tokens" in body
    # 4.821 + 31.42 + 0.182 ETH, valued at 3120.55
    assert "$113,660" in body, "native value is the sum across every account"
    assert "36.423" in body
    assert "1,750.5" in body, "USDC held across two wallets shows as one holding"


def test_summary_aggregates_one_asset_across_accounts(client):
    payload = client.get("/api/portfolio").json()
    usdc = next(a for a in payload["assets"] if a["symbol"] == "USDC")

    # Cold holds 500, Main holds 1,250.50
    assert usdc["amount"] == "1,750.5"
    assert payload["priced"] is True


def test_summary_narrows_to_the_selected_wallet(client):
    cold = account_id(client, "Cold")
    everything = client.get("/api/portfolio").json()
    just_cold = client.get("/api/portfolio", params={"wallet": cold}).json()

    assert everything["scope"] == "My wallets" and everything["accounts"] == 3
    assert just_cold["scope"] == "Cold" and just_cold["accounts"] == 1
    assert just_cold["total"] != everything["total"]
    assert {a["symbol"] for a in just_cold["assets"]} == {"ETH", "USDC", "FREE-AIRDROP"}


def test_holdings_are_ordered_by_value_with_dust_last(client):
    payload = client.get("/api/portfolio").json()

    assert payload["assets"][0]["symbol"] == "ETH", "the largest holding leads"
    assert payload["assets"][-1]["value"] is None, "dust never looks like the biggest"
    assert payload["hidden_assets"] == 1, "the airdrop is kept but not shown"
    assert payload["unpriced_assets"] == 0, "nothing worth showing lacks a price"


def test_dust_is_hidden_but_reachable(client):
    """A monitor must not quietly drop real on-chain state: the dust is counted,
    named and one click from being shown."""
    body = client.get("/summary").text

    assert "1 hidden" in body
    assert "toggleHiddenTokens()" in body
    assert "when-hidden" in body, "and it reads as something to press"

    # And it belongs to the token count, not to the total: under TOTAL VALUE it
    # read as though the hidden ones were missing from the figure above it.
    total, _, tokens = body.partition('<span class="figure__label">Tokens</span>')
    assert "toggleHiddenTokens()" not in total
    assert "holding--hidden" in body, "still rendered, just not displayed"
    assert "FREE-AIRDROP" in body


def test_a_holding_click_filters_the_feed_by_that_asset(client):
    body = client.get("/summary").text
    assert 'hx-get="/activity?asset=' in body
    assert "filterByAsset(" in body

    usdc = next(row["id"] for row in client.ctx.db.feed_assets() if row["symbol"] == "USDC")
    feed = client.get("/activity", params={"asset": usdc}).text
    assert feed.count("data-activity=") == 1


def test_the_sidebar_abbreviates_each_value(client):
    """Precision here would be noise; the exact figure is one click away."""
    body = client.get("/wallets").text
    assert re.search(r'class="wallet__value num" title="\$[\d,]+">\s*\$[\d.]+k', body)


def test_the_collapsed_summary_is_one_line(client):
    """Collapsing is CSS on <html>, so the fragment always carries both the
    one-line brief and the detail and nothing has to re-render to switch."""
    body = client.get("/summary").text
    assert 'class="summary__brief"' in body
    assert 'class="summary__body"' in body
    assert "toggleSummary()" in client.get("/").text


def test_valuation_absent_is_stated_not_faked(settings, watch_config, provider, sample_transfers):
    """With no price source the panel says so instead of showing a total."""
    provider.transfers = sample_transfers
    app = create_app(settings=settings, config=watch_config, provider=provider, run_indexer=False)
    with TestClient(app) as test_client:
        run(app.state.ctx.indexer.run_once())
        body = test_client.get("/summary").text
        payload = test_client.get("/api/portfolio").json()

        assert "valuation off" in body
        assert payload["total"] is None and payload["priced"] is False
        assert "$0" not in body, "a missing valuation must never render as zero"
        assert "36.423" in body, "amounts still show without prices"


def test_scope_links_do_not_inherit_a_stale_wallet_value(client):
    """The wallets section sets hx-include so its polling carries the current
    scope. htmx inherits that down to the links inside it, where it would append
    the value as it was *before* the click and override the scope in the link's
    own URL — so each link cancels the inheritance."""
    body = client.get("/wallets").text

    for link in re.findall(r'<a class="wallet(?:-all|__hit)[^"]*"[^>]*>', body):
        assert 'hx-include="this"' in link, link


def test_a_holding_keeps_the_current_scope(client):
    """Clicking an asset narrows by asset; it must not silently widen the feed
    back to every account, so it carries the scope along."""
    body = client.get("/summary").text
    link = re.search(r'<a class="holding__hit"[^>]*>', body).group(0)
    assert 'hx-include="#wallet-value, #scope-value"' in link


def test_the_summary_names_its_scope(client):
    """All wallets carries a count; one wallet carries its address, which the
    collapsed line drops so the value stays the answer."""
    everything = client.get("/summary").text
    assert ">My wallets</span>" in everything
    assert 'class="summary__count">3 accounts</span>' in everything
    assert 'class="summary__addr"' not in everything

    cold = client.get("/summary", params={"wallet": account_id(client, "Cold")}).text
    assert ">Cold</span>" in cold
    assert re.search(r'class="summary__addr"[^>]*>0x[0-9a-fA-F]{4}…[0-9a-fA-F]{4}</button>', cold)
    assert 'class="summary__count"' not in cold


def test_erc20_holdings_are_called_tokens_not_assets(client):
    """NATIVE is an asset too, so "4 assets" under TOKENS reads as the whole
    portfolio. These four are ERC-20s."""
    body = client.get("/summary").text
    tokens = re.search(r'<span class="figure__label">Tokens</span>.*?</div>', body, re.S).group(0)

    assert re.search(r"\d+ (tokens?|shown)", tokens)
    assert "asset" not in tokens


def test_price_freshness_is_a_footnote_not_a_metric(client):
    body = client.get("/summary").text
    assert 'class="holdings__age"' in body
    assert body.count('class="figure__label"') == 3, "total, native, tokens — and nothing else"
    assert "PRICES" not in body.upper().replace("PRICES <SPAN", "")


def test_the_collapsed_line_leads_with_the_value(client):
    """Collapsed, the panel answers "how much" in one line: the total is the
    only bright thing, the scope and the breakdown are context."""
    body = client.get("/summary").text
    order = [
        body.index('class="summary__scope"'),
        body.index('class="summary__count"'),
        body.index('class="summary__total'),
        body.index('class="summary__brief"'),
    ]
    assert order == sorted(order)

    css = (pathlib.Path("app/web/static/app.css")).read_text()
    assert ".summary__total {" in css and "--text-hi" in css.split(".summary__total {")[1][:200]


def test_the_columns_are_proportioned_not_packed(client):
    """Only the clerical columns are pinned; the ones carrying meaning are given
    a share of the width, so a wide screen is used rather than left blank."""
    css = (pathlib.Path("app/web/static/app.css")).read_text()
    feed_rules = css.split(".feed {")[1].split("}")[0]

    assert "table-layout: fixed" in feed_rules
    assert "max-width" not in feed_rules, "a capped table cuts the row separators"
    assert "margin" not in feed_rules, "no auto margins: the table stays left-aligned"
    for pinned in (".cell-time", ".cell-dir"):
        assert re.search(rf"\{pinned} +\{{ width: \d+px", css), pinned
    for proportional in (".cell-wallet", ".cell-value", ".cell-party", ".cell-tx"):
        assert re.search(rf"\{proportional} +\{{ width: \d+%", css), proportional

    # Counterparty is the widest: on real data it is the least abbreviable.
    # Amount is not below the hash any more — a hash reads fine shortened, and
    # "$1,31…" is a number the reader cannot use.
    widths = {
        name: int(re.search(rf"\.cell-{name} +\{{ width: (\d+)%", css).group(1))
        for name in ("wallet", "value", "party", "tx")
    }
    assert widths["party"] > widths["value"] >= widths["tx"] > widths["wallet"]


def test_the_fiat_beside_an_amount_is_never_clipped(client):
    """An amount and what it was worth are one fact. Truncating the second half
    of it to "$1,31…" is worse than not showing it at all."""
    css = (pathlib.Path("app/web/static/app.css")).read_text()
    rule = css.split(".cell-value { overflow:")[1].split("}")[0]
    assert "visible" in rule


def test_an_internal_transfer_says_what_it_is(client):
    """"int" is short enough for a dense row and opaque on its own. The words
    belong where there is room to read them."""
    rows = client.ctx.db.query_activities(limit=200)
    internal = next(r for r in rows if r["kind"] == "internal")

    assert "int" in client.get("/activity").text
    body = client.get(f"/tx/{internal['id']}").text
    assert "moved by the contract this transaction called" in body


# ------------------------------------------------------- owned vs watched


@pytest.fixture
def mixed_client(settings, provider, prices, sample_transfers):
    """Two owned wallets and one merely watched, with a transfer between an
    owned wallet and the watched one."""
    config = make_config(
        wallets=[
            {"name": "Main", "address": MAIN},
            {"name": "Cold", "address": COLD},
            {"name": "Whale", "address": STRANGER, "owned": False},
        ]
    )
    provider.transfers = sample_transfers
    provider.native_balances[STRANGER] = 900 * 10**18
    app = create_app(settings=settings, config=config, provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as test_client:
        run(app.state.ctx.indexer.run_once())
        test_client.ctx = app.state.ctx
        yield test_client


def rows_of(body: str) -> list[str]:
    """Activity rows only: not the day markers, and not the folded siblings."""
    found = re.findall(r'<tr class="row row--[^"]*"\s+data-activity.*?</tr>', body, re.S)
    return [row for row in found if "row--folded" not in row]


def test_the_sidebar_separates_mine_from_watched(mixed_client):
    body = mixed_client.get("/wallets").text
    groups = re.findall(r'class="wallets__group[^"]*"[^>]*>\s*<span>([^<]+)</span>', body)

    assert [g.strip() for g in groups] == ["All activity", "My wallets", "Watching"]
    assert body.index(">Main</span>") < body.index("Watching")
    assert body.index(">Whale</span>") > body.index("Watching")
    assert body.index(">Whale</span>") > body.index("Watching")


def test_a_watched_wallet_is_not_counted_in_the_total(mixed_client):
    payload = mixed_client.get("/api/portfolio").json()

    assert payload["scope"] == "My wallets"
    assert payload["accounts"] == 2, "the whale is watched, not owned"
    symbols = {a["symbol"]: a["amount"] for a in payload["assets"]}
    # Main 4.821 + Cold 31.42; the whale's 900 ETH is nowhere in it.
    assert symbols["ETH"] == "36.241"


def test_a_watched_wallet_still_has_a_full_wallet_view(mixed_client):
    whale = next(a for a in mixed_client.ctx.db.list_accounts() if a.name == "Whale")
    payload = mixed_client.get("/api/portfolio", params={"wallet": whale.id}).json()

    assert payload["scope"] == "Whale"
    assert payload["assets"][0]["symbol"] == "ETH"
    assert payload["assets"][0]["amount"] == "900"
    assert mixed_client.get("/activity", params={"wallet": whale.id}).text.count("data-activity=")


def test_all_activity_includes_watched_wallets(mixed_client):
    everything = mixed_client.get("/activity").text
    mine = mixed_client.get("/activity", params={"scope": "mine"}).text

    assert "Whale" in everything
    assert len(rows_of(everything)) > len(rows_of(mine))

    # The whale's transfer to an address none of my wallets touch (0.05 ETH) is
    # indexed and visible in All activity, and out of scope for My wallets. It
    # still appears as a *counterparty* there, which is a different thing.
    assert any("0.05" in row for row in rows_of(everything))
    assert not any("0.05" in row for row in rows_of(mine))


def test_one_movement_reads_differently_from_each_point_of_view(mixed_client):
    """A transfer between two known accounts is one row. Whether it reads as
    `Main → Whale` or as an IN/OUT depends on how many of its ends are in view."""
    ctx = mixed_client.ctx
    accounts = {a.name: a.id for a in ctx.db.list_accounts()}

    everything = mixed_client.get("/activity").text
    from_main = mixed_client.get("/activity", params={"wallet": accounts["Main"]}).text
    from_whale = mixed_client.get("/activity", params={"wallet": accounts["Whale"]}).text

    # 2.4 ETH moved from the whale's address to Main.
    shared = [row for row in rows_of(everything) if "2.4" in row]
    assert len(shared) == 1, "one movement, one row"
    assert 'class="pair">Whale<span class="arrow">&rarr;</span>Main<' in shared[0]

    seen_by_main = next(row for row in rows_of(from_main) if "2.4" in row)
    assert ">IN<" in seen_by_main and ">Whale</a>" in seen_by_main
    assert 'class="pair"' not in seen_by_main

    seen_by_whale = next(row for row in rows_of(from_whale) if "2.4" in row)
    assert ">OUT<" in seen_by_whale and ">Main</a>" in seen_by_whale


def test_my_wallets_view_treats_a_watched_counterparty_as_external(mixed_client):
    """Inside "my wallets" the whale is not in view, so a transfer to it is an
    ordinary outgoing one — not a pair."""
    mine = mixed_client.get("/activity", params={"scope": "mine"}).text
    row = next(row for row in rows_of(mine) if "2.4" in row)

    assert ">IN<" in row
    assert 'class="pair"' not in row
    assert ">Whale</a>" in row, "it is still named, just not treated as mine"


def test_two_owned_wallets_still_pair_up_in_my_wallets(mixed_client):
    mine = mixed_client.get("/activity", params={"scope": "mine"}).text
    paired = [row for row in rows_of(mine) if 'class="pair"' in row]

    assert len(paired) == 1
    assert "Main" in paired[0] and "Cold" in paired[0]


# ------------------------------------------------------- adding from the UI


def test_a_wallet_can_be_added_from_the_page(client):
    response = client.post(
        "/wallets",
        data={"name": "Whale #1", "address": STRANGER, "chain": "ethereum", "owned": "on"},
        headers={"sec-fetch-site": "same-origin"},
    )

    assert response.status_code == 200
    assert response.headers["hx-trigger"] == "wallet-changed", "the dialog closes on this"
    added = next(a for a in client.ctx.db.list_accounts() if a.name == "Whale #1")
    assert added.is_owned is True and added.source == "ui"
    assert ">Whale #1</span>" in client.get("/wallets").text


def test_an_added_wallet_left_out_of_the_portfolio_lands_in_watching(client):
    client.post(
        "/wallets",
        data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},  # checkbox unticked
        headers={"sec-fetch-site": "same-origin"},
    )
    body = client.get("/wallets").text

    assert body.index("Watching") < body.index(">Whale</span>")
    assert client.get("/api/portfolio").json()["accounts"] == 3, "still three of mine"


def test_an_added_wallet_is_indexed_from_the_backfill_window(client, provider):
    """It must not silently start from the current head."""
    client.post(
        "/wallets",
        data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},
        headers={"sec-fetch-site": "same-origin"},
    )
    provider.calls.clear()
    run(client.ctx.indexer.run_once())

    starts = {address: from_block for _, address, _, from_block, _ in provider.calls}
    assert starts[STRANGER] < starts[MAIN]


def test_adding_a_wallet_attributes_history_already_indexed(client):
    """Transfers already stored because one of my wallets was involved should
    show the new account by name straight away, without waiting for a sync."""
    client.post(
        "/wallets",
        data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},
        headers={"sec-fetch-site": "same-origin"},
    )
    body = client.get("/activity").text
    assert 'class="pair">Whale' in body or ">Whale</a>" in body


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"name": "", "address": STRANGER}, "name"),
        ({"name": "Bad", "address": "0x123"}, "not a valid Ethereum address"),
        ({"name": "Bad", "address": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"}, "not a valid"),
        ({"name": "Bad", "address": "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed"}, "checksum"),
    ],
)
def test_a_bad_address_is_refused_with_a_reason(client, payload, message):
    before = len(client.ctx.db.list_accounts())
    response = client.post(
        "/wallets",
        data={"chain": "ethereum", **payload},
        headers={"sec-fetch-site": "same-origin"},
    )

    assert response.status_code == 422
    assert message.lower() in response.text.lower()
    assert len(client.ctx.db.list_accounts()) == before


def test_an_address_already_watched_is_refused(client):
    response = client.post(
        "/wallets",
        data={"name": "Again", "address": MAIN, "chain": "ethereum"},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 422
    assert "already watched" in response.text


def test_a_cross_site_post_is_rejected(client):
    response = client.post(
        "/wallets",
        data={"name": "Evil", "address": STRANGER, "chain": "ethereum"},
        headers={"sec-fetch-site": "cross-site"},
    )

    assert response.status_code == 403
    assert "Evil" not in [a.name for a in client.ctx.db.list_accounts()]


def test_only_ui_added_wallets_offer_editing(client):
    assert "Remove from blocktail" not in client.get("/wallets").text

    client.post(
        "/wallets",
        data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},
        headers={"sec-fetch-site": "same-origin"},
    )
    body = client.get("/wallets").text
    assert body.count("Remove from blocktail") == 1
    assert body.count("Move to Watching") + body.count("Include in portfolio") == 1

    whale = next(a for a in client.ctx.db.list_accounts() if a.name == "Whale")
    removed = client.post(
        f"/wallets/{whale.id}/remove", headers={"sec-fetch-site": "same-origin"}
    )
    assert removed.status_code == 200
    assert "Whale" not in [a.name for a in client.ctx.db.list_accounts()]
    assert client.ctx.db.activity_count() > 0, "history is kept"


def test_a_config_declared_wallet_cannot_be_removed_from_the_page(client):
    main = next(a for a in client.ctx.db.list_accounts() if a.name == "Main")
    response = client.post(
        f"/wallets/{main.id}/remove", headers={"sec-fetch-site": "same-origin"}
    )

    assert response.status_code == 422
    assert "config file" in response.text
    assert "Main" in [a.name for a in client.ctx.db.list_accounts()]


def test_the_top_line_counts_and_totals_only_what_is_mine(mixed_client):
    """The panel says "my wallets"; the top line must not contradict it by
    folding a watched whale's balance into the same figure."""
    body = mixed_client.get("/status").text

    assert "2 mine" in body and "1 watching" in body
    assert "36.241" in body, "Main + Cold, without the whale's 900 ETH"
    assert "936" not in body


def test_the_top_line_stays_plain_when_nothing_is_merely_watched(client):
    assert "3 accounts" in client.get("/status").text
    summary = re.search(r'<span class="summary".*?</span>', client.get("/status").text, re.S).group(0)
    assert "mine" not in summary and "watching" not in summary


def test_ownership_can_be_toggled_from_the_menu(client):
    """"Leave ownership alone" and "make it watched" must not look the same on
    the wire: an unticked checkbox sends nothing at all, so the field is
    explicit rather than a checkbox."""
    client.post("/wallets", data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},
                headers={"sec-fetch-site": "same-origin"})
    whale = next(a for a in client.ctx.db.list_accounts() if a.name == "Whale")
    assert whale.is_owned is False

    promoted = client.post(f"/wallets/{whale.id}", data={"set_owned": "true"},
                           headers={"sec-fetch-site": "same-origin"})
    assert promoted.status_code == 200
    assert next(a for a in client.ctx.db.list_accounts() if a.name == "Whale").is_owned is True

    demoted = client.post(f"/wallets/{whale.id}", data={"set_owned": "false"},
                          headers={"sec-fetch-site": "same-origin"})
    assert demoted.status_code == 200
    assert next(a for a in client.ctx.db.list_accounts() if a.name == "Whale").is_owned is False


def test_renaming_leaves_ownership_alone(client):
    client.post("/wallets", data={"name": "Whale", "address": STRANGER, "chain": "ethereum",
                                  "owned": "on"},
                headers={"sec-fetch-site": "same-origin"})
    whale = next(a for a in client.ctx.db.list_accounts() if a.name == "Whale")

    client.post(f"/wallets/{whale.id}", data={"name": "Whale renamed"},
                headers={"sec-fetch-site": "same-origin"})

    renamed = next(a for a in client.ctx.db.list_accounts() if a.id == whale.id)
    assert renamed.name == "Whale renamed"
    assert renamed.is_owned is True, "a rename must not silently reclassify the wallet"


def test_an_empty_rename_is_refused(client):
    client.post("/wallets", data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},
                headers={"sec-fetch-site": "same-origin"})
    whale = next(a for a in client.ctx.db.list_accounts() if a.name == "Whale")

    response = client.post(f"/wallets/{whale.id}", data={"name": "   "},
                           headers={"sec-fetch-site": "same-origin"})

    assert response.status_code == 422
    assert next(a for a in client.ctx.db.list_accounts() if a.id == whale.id).name == "Whale"


def test_a_removed_wallet_resumes_rather_than_reindexes(client):
    """Archived, not deleted: re-adding the address keeps its watermark, so the
    indexer picks up where it stopped instead of replaying the whole history."""
    client.post("/wallets", data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},
                headers={"sec-fetch-site": "same-origin"})
    whale = next(a for a in client.ctx.db.list_accounts() if a.name == "Whale")
    client.ctx.db.mark_synced({whale.id: 21_000_000}, 21_000_050)

    client.post(f"/wallets/{whale.id}/remove", headers={"sec-fetch-site": "same-origin"})
    assert "Whale" not in [a.name for a in client.ctx.db.list_accounts()]

    client.post("/wallets", data={"name": "Whale again", "address": STRANGER, "chain": "ethereum"},
                headers={"sec-fetch-site": "same-origin"})
    back = next(a for a in client.ctx.db.list_accounts() if a.address == STRANGER)

    assert back.id == whale.id, "the archived row came back, it was never deleted"
    assert back.synced_to_block == 21_000_050
    assert back.name == "Whale again"


def test_a_watched_wallet_says_so_in_both_summary_states(mixed_client):
    """Opening someone else's address must never look like opening your own —
    including in the one-line collapsed form, where the value is prominent."""
    whale = next(a for a in mixed_client.ctx.db.list_accounts() if a.name == "Whale")
    body = mixed_client.get("/summary", params={"wallet": whale.id}).text

    assert 'class="summary__badge">watching</span>' in body
    head = re.search(r'<div class="summary__head">.*?</div>', body, re.S).group(0)
    assert "summary__badge" in head, "it rides with the name on the same line"
    assert "summary__scope" in head

    css = pathlib.Path("app/web/static/app.css").read_text()
    assert ".summary-collapsed .summary__badge" not in css, "it must not hide when collapsed"

    mine = next(a for a in mixed_client.ctx.db.list_accounts() if a.name == "Main")
    assert 'class="summary__badge">my wallet</span>' in (
        mixed_client.get("/summary", params={"wallet": mine.id}).text
    )


# -------------------------------------------------------------- inspector


def test_a_row_opens_the_transaction_behind_it(client):
    """The feed drops the hash and block to stay readable. They are not lost:
    they are in the inspector, beside the rest of the transaction."""
    row_id = client.ctx.db.query_activities(limit=1)[0]["id"]
    body = client.get(f"/tx/{row_id}").text

    assert re.search(r"0x[0-9a-f]{64}", body), "the full transaction hash"
    assert "etherscan.io/tx/" in body and "etherscan.io/block/" in body
    assert "Transfers" in body and "#" in body and "conf" in body


def _a_swap(client):
    """One transaction, three transfers — what a swap actually looks like."""
    client.ctx.provider.transfers = [
        transfer("swap", sender=MAIN, recipient=STRANGER, amount=200 * 10**6,
                 asset=USDC, kind=TransferKind.TOKEN, log_index=3, block=21_000_400),
        transfer("swap", sender=MAIN, recipient=STRANGER, amount=368_040,
                 asset=USDC, kind=TransferKind.TOKEN, log_index=4, block=21_000_400),
        transfer("swap", sender=STRANGER, recipient=MAIN, amount=5 * 10**16,
                 kind=TransferKind.INTERNAL, trace="0_1", block=21_000_400),
    ]
    client.ctx.provider.head_block = 21_000_410
    run(client.ctx.indexer.run_once())
    rows = [r for r in client.ctx.db.query_activities(limit=200)
            if r["block_number"] == 21_000_400]
    assert len(rows) == 3
    return rows


def test_the_inspector_shows_every_transfer_of_one_transaction(client):
    """A swap is one transaction and several transfers. The feed keeps them
    apart because that is the honest unit; the inspector puts them back in one
    place, without inventing a total by adding unlike things together."""
    rows = _a_swap(client)
    body = client.get(f"/tx/{rows[0]['id']}").text

    assert body.count('class="xfer ') == 3
    assert '<span class="insp__n num">3</span>' in body
    assert "$" not in body.split("Transfers")[0].split("Depth")[-1], "no invented total"


def test_the_inspector_marks_the_row_you_came_from(client):
    rows = _a_swap(client)
    row_id = rows[-1]["id"]

    body = client.get(f"/tx/{row_id}").text
    assert body.count("is-current") == 1
    assert f'data-selected="{row_id}"' in body


def test_opening_a_missing_row_says_so(client):
    response = client.get("/tx/999999")
    assert response.status_code == 404
    assert "gone" in response.text


def test_the_narrow_layout_is_not_a_squeezed_desktop(client):
    """Below the breakpoint the sidebar is a sheet, the summary starts closed
    and the feed is a log — so the first screen shows activity, not navigation."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]

    assert "position: fixed" in narrow and "translateY(101%)" in narrow, "the sidebar docks away"
    assert ".scope-trigger {" in narrow, "and is reached through a selector"
    assert "grid-template-areas" in narrow, "rows are laid out, not tabulated"
    assert ".cell-tx, .cell-fill { display: none; }" in narrow

    base = pathlib.Path("app/web/templates/base.html").read_text()
    assert "max-width: 860px" in base, "the summary starts collapsed on a narrow screen"


def test_a_row_is_clickable_without_swallowing_its_links(client):
    """The row opens the transaction; the links inside it keep their own jobs.
    One trigger filter does that, rather than a stopPropagation on each link."""
    body = client.get("/activity").text
    assert "click[!event.target.closest('a,button')]" in body
    assert 'hx-get="/tx/' in body


def test_the_narrow_scope_selector_names_what_the_feed_shows(client):
    """The panel always totals what is mine; the feed's scope is a separate
    thing, so the selector needs its own label rather than the panel's."""
    assert ">All activity</span>" in client.get("/summary").text
    assert ">Watching</span>" in client.get("/summary", params={"scope": "watched"}).text
    assert ">My wallets</span>" in client.get("/summary", params={"scope": "mine"}).text

    cold = account_id(client, "Cold")
    assert ">Cold</span>" in client.get("/summary", params={"wallet": cold}).text

    # ...and it must actually be sent the scope, or it can only ever say "all".
    assert 'hx-include="#wallet-value, #scope-value"' in client.get("/summary").text


def test_watching_is_its_own_scope(mixed_client):
    watched = mixed_client.get("/activity", params={"scope": "watched"}).text
    mine = mixed_client.get("/activity", params={"scope": "mine"}).text

    assert "Whale" in watched
    assert "Main</td>" not in watched.replace(" ", ""), "my wallets are out of this scope"
    assert len(rows_of(watched)) < len(rows_of(mine))


def test_choosing_the_watched_group_shows_its_own_total(client):
    """The panel ignores how wide the *feed* is set — a whale must never land in
    "my total". But choosing the watched group is a deliberate question, and
    answering it with someone else's number would just be wrong."""
    client.post("/wallets", data={"name": "Whale", "address": STRANGER, "chain": "ethereum"},
                headers={"sec-fetch-site": "same-origin"})

    everything = client.get("/api/portfolio").json()
    watched = client.get("/api/portfolio", params={"scope": "watched"}).json()

    assert everything["scope"] == "My wallets" and everything["accounts"] == 3
    assert watched["scope"] == "Watching" and watched["accounts"] == 1
    assert "not mine" in client.get("/summary", params={"scope": "watched"}).text


def test_the_page_scrolls_rather_than_an_inner_region(client):
    """An inner scroll region turns the page into an embedded terminal: a static
    document with a small pane moving inside it. The page scrolls; the chrome
    stays put by being sticky."""
    css = pathlib.Path("app/web/static/app.css").read_text()

    assert ".table-scroll { }" in css, "no scroll container around the table"
    assert "overflow: hidden" not in css.split("@media (min-width: 861px) {")[1].split("\n}")[0]
    assert "top: var(--chrome-h)" in css, "the table head sticks under the chrome instead"

    rows = pathlib.Path("app/web/templates/_rows.html").read_text()
    assert 'hx-trigger="intersect once"' in rows


def test_the_desktop_content_is_bounded_and_centred(client):
    """Past roughly 1440px the parts of a row drift far enough apart that
    scanning becomes head-turning. The limit is on width only — the viewport
    keeps its own background, with no card, border or shadow around the app."""
    assert '<div class="app">' in client.get("/").text

    css = pathlib.Path("app/web/static/app.css").read_text()
    shell = css.split("@media (min-width: 861px) {")[1].split("\n}")[0]

    assert "width: min(100% - 40px, 1440px)" in shell
    assert "margin-inline: auto" in shell
    assert "box-shadow" not in shell, "a shadow would lift it off the page"
    assert "background: var(--panel)" not in shell, "no band of fill ending at the edge"


def test_the_activity_area_has_a_right_hand_edge(client):
    """A sidebar on the left and unbounded text on the right reads as an
    unfinished composition. The outline is a boundary, not a card: the fill is
    still the page background."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    shell = css.split("@media (min-width: 861px) {")[1].split("\n}")[0]

    assert ".layout {" in shell and "border: 1px solid var(--border)" in shell
    assert ".topbar, .filters { background: var(--bg); }" in shell, (
        "panel bands ending at the container edge read as a card"
    )


def test_the_narrow_layout_keeps_the_full_width(client):
    """The bound is a desktop concern; a phone has no width to spare."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]
    assert ".app" not in narrow, "the container is left alone below the breakpoint"


def test_one_absurd_asset_cannot_swallow_the_total(settings, provider, prices, sample_transfers):
    """Live data found this: a scam token with a nominal DEX quote and 10^17
    units made the portfolio read $4.9 quadrillion. An asset that would be
    almost the entire total on its own is excluded and named."""
    from decimal import Decimal

    from app.main import create_app
    from app.models import AssetRef, Balance

    scam = AssetRef("ethereum", "0x" + "5" * 40, "SCAM", 18)
    provider.transfers = sample_transfers
    provider.token_balances[MAIN] = [Balance(scam, 10**17 * 10**18)]
    prices.quotes[("ethereum", scam.contract_address)] = Decimal("0.049")

    app = create_app(settings=settings, config=make_config(), provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        payload = client.get("/api/portfolio").json()

        assert payload["excluded_assets"] == ["SCAM"]
        assert "SCAM" in client.get("/summary").text
        # What is left is the honest part: ETH across the owned wallets.
        assert payload["total"] is not None
        assert "quadrillion" not in payload["total"]
        assert int(payload["total"].lstrip("$").replace(",", "")) < 10**9


def test_a_normal_concentrated_portfolio_is_left_alone(client):
    """The guard must not fire on someone who simply holds mostly one thing —
    the native asset is never a candidate, and neither is a lone holding."""
    payload = client.get("/api/portfolio").json()
    assert payload["excluded_assets"] == []
    assert payload["total"] is not None


def test_an_absurd_holding_cannot_climb_over_its_neighbour(client):
    """Live data again: a token with 10^17 units overflowed its grid cell and
    printed on top of the next column. Grid children need telling to shrink."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    rules = css.split(".holding__hit {")[1].split("}")[0]

    assert "minmax(0," in rules, "columns must be allowed to shrink"
    assert ".holding__hit > span { min-width: 0; overflow: hidden;" in css

    # The full figure stays reachable rather than being merely truncated.
    assert re.search(r'class="holding__hit"[^>]*title="[\d,.]+ \w+', client.get("/summary").text)


# ------------------------------------------------------------- dust filtering


def _portfolio_with(client, **balances):
    """Re-run the indexer with a given set of token balances."""
    from decimal import Decimal

    from app.models import AssetRef, Balance

    provider = client.ctx.provider
    # Clear every account, so the assertion is about the tokens under test and
    # not about whatever the shared fixture happens to hold.
    for address in list(provider.token_balances):
        provider.token_balances[address] = []
    made = []
    for symbol, (contract, amount, price) in balances.items():
        asset = AssetRef("ethereum", contract, symbol, 18)
        made.append(Balance(asset, amount))
        if price is not None:
            client.ctx.indexer.price_source.quotes[("ethereum", contract)] = Decimal(price)
    provider.token_balances[MAIN] = made
    client.ctx.indexer._priced_at = None
    run(client.ctx.indexer.run_once())
    return client.get("/api/portfolio").json()


def test_a_token_is_never_judged_by_its_balance(client):
    """1 WBTC and 1 scam coin are the same number. Supplies are arbitrary, so a
    raw balance carries no information about whether a holding is real."""
    payload = _portfolio_with(
        client,
        WBTC=("0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", 1, None),   # known, no price
        SCAM=("0x" + "e" * 40, 1, None),                                 # identical balance
    )
    shown = {a["symbol"] for a in payload["assets"] if a["value"] or a["symbol"] == "WBTC"}

    assert "WBTC" in shown, "a known token survives having no price and a balance of 1"
    assert payload["hidden_assets"] >= 1


def test_a_token_worth_something_is_always_shown(client):
    """Value alone is enough; nothing else has to vouch for it."""
    payload = _portfolio_with(client, ODDCOIN=("0x" + "d" * 40, 5 * 10**18, "3.50"))
    symbols = [a["symbol"] for a in payload["assets"]]

    assert "ODDCOIN" in symbols
    assert payload["hidden_assets"] == 0


def test_dust_you_have_spent_is_shown(client, provider, sample_transfers):
    """Spam only ever arrives. Having sent something is the strongest signal
    available that its holder considers it real."""
    from app.models import TransferKind

    spam_contract = "0x" + "f" * 40
    # Inside the re-scan window: the fixture has already synced once, so an
    # older block would simply not be looked at again.
    provider.transfers = [
        *sample_transfers,
        transfer("spent-it", sender=MAIN, recipient=STRANGER, amount=10**18,
                 asset=AssetRef("ethereum", spam_contract, "ODD", 18),
                 kind=TransferKind.TOKEN, log_index=7, block=21_000_095),
    ]
    run(client.ctx.indexer.run_once())

    payload = _portfolio_with(client, ODD=(spam_contract, 10**18, None))
    shown = [a["symbol"] for a in payload["assets"][: len(payload["assets"]) - payload["hidden_assets"]]]
    assert "ODD" in shown, "it has no price and no reputation, but you spent it"


def test_a_vouched_for_token_is_shown(settings, provider, prices, sample_transfers):
    """`trusted_assets` is the manual override, for the token no rule catches."""
    from app.main import create_app

    contract = "0x" + "c" * 40
    provider.transfers = sample_transfers
    provider.token_balances[MAIN] = [Balance(AssetRef("ethereum", contract, "MINE", 18), 1)]
    config = make_config(trusted_assets=[contract])

    app = create_app(settings=settings, config=config, provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        payload = client.get("/api/portfolio").json()

    shown = [a["symbol"] for a in payload["assets"][: len(payload["assets"]) - payload["hidden_assets"]]]
    assert "MINE" in shown, "vouched for in the config, so shown regardless"


def test_hidden_tokens_are_still_indexed(client):
    """Filtering is presentation only: the chain data stays whole."""
    before = client.ctx.db.activity_count()
    payload = client.get("/api/portfolio").json()

    assert payload["hidden_assets"] == 1
    assert client.ctx.db.activity_count() == before
    assert any(row["symbol"] == "FREE-AIRDROP" for row in client.ctx.db.balances_by_account()[
        next(a.id for a in client.ctx.db.list_accounts() if a.name == "Cold")
    ]), "the balance is still stored"


def test_the_asset_filter_separates_the_unrecognised(client):
    """`All assets` must not become a list of three hundred scam tokens."""
    body = client.get("/").text
    assert "<optgroup" not in body or "Unrecognised" in body

    usdc = next(row["id"] for row in client.ctx.db.feed_assets() if row["symbol"] == "USDC")
    assert f'value="{usdc}"' in body, "a known token is directly selectable"


def test_the_activity_feed_is_not_filtered(client):
    """An unexpected token arriving *is* activity. Holdings and the feed are
    filtered separately, and the feed not at all."""
    feed = client.get("/activity").text
    assert "USDC" in feed
    assert client.get("/api/activity").json()["activities"], "nothing dropped from the log"


# ------------------------------------------------------------ dust transfers


@pytest.fixture
def dusty_client(settings, watch_config, provider, prices, sample_transfers):
    """A feed with real transfers and the sub-cent noise that arrives beside
    them on a live address."""
    provider.transfers = [
        *sample_transfers,
        transfer("dust-1", sender=STRANGER, recipient=MAIN, amount=59, asset=USDC,
                 kind=TransferKind.TOKEN, log_index=11, block=21_000_051),
        transfer("dust-2", sender=STRANGER, recipient=MAIN, amount=23, asset=USDC,
                 kind=TransferKind.TOKEN, log_index=12, block=21_000_052),
    ]
    app = create_app(settings=settings, config=watch_config, provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as test_client:
        run(app.state.ctx.indexer.run_once())
        test_client.ctx = app.state.ctx
        yield test_client


def test_dust_transfers_are_out_of_the_feed_by_default(dusty_client):
    """0.000059 USDT next to a 594 USDT payment gets nearly the same visual
    weight, and on a live address there are a great many of them."""
    payload = dusty_client.get("/api/activity").json()
    amounts = [a["amount"] for a in payload["activities"]]

    assert "0.000059" not in amounts and "0.000023" not in amounts
    assert "500" in amounts, "the real USDC transfer stays"
    assert dusty_client.ctx.db.activity_count() == 7, "and nothing was deleted"


def test_the_feed_says_how_much_it_is_hiding(dusty_client):
    body = dusty_client.get("/").text
    assert "2 noisy transfers hidden" in body and "2 dust" in body
    assert "toggleDust(true)" in body


def test_dust_can_be_shown(dusty_client):
    shown = dusty_client.get("/api/activity", params={"dust": "show"}).json()["activities"]
    assert "0.000059" in [a["amount"] for a in shown]
    assert "Showing dust and unverified" in dusty_client.get(
        "/", params={"dust": "show"}
    ).text


def test_dust_is_never_decided_by_the_raw_amount(dusty_client, provider, prices):
    """59 raw units of a 6-decimal stablecoin is dust; 59 raw units of an
    unpriced token is unknown, and unknown is not dust."""
    from decimal import Decimal

    from app.models import AssetRef

    # A token the chain *is* known for, so only the dust rule can act on it.
    mystery = AssetRef("ethereum", "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", "WBTC", 6)
    # Only held assets get priced, so give it a balance too — otherwise there is
    # no price and therefore, correctly, no verdict either way.
    provider.token_balances[MAIN] = [*provider.token_balances.get(MAIN, []), Balance(mystery, 59)]
    provider.transfers = [
        *provider.transfers,
        transfer("mystery", sender=STRANGER, recipient=MAIN, amount=59, asset=mystery,
                 kind=TransferKind.TOKEN, log_index=13, block=21_000_095),
    ]
    run(dusty_client.ctx.indexer.run_once())

    symbols = [a["asset"]["symbol"] for a in dusty_client.get("/api/activity").json()["activities"]]
    assert "WBTC" in symbols, "no price means no dust verdict"

    # Give it a price and the same transfer becomes dust.
    prices.quotes[("ethereum", mystery.contract_address)] = Decimal("1")
    dusty_client.ctx.indexer._priced_at = None
    run(dusty_client.ctx.indexer.run_once())
    symbols = [a["asset"]["symbol"] for a in dusty_client.get("/api/activity").json()["activities"]]
    assert "WBTC" not in symbols


def test_hiding_dust_does_not_shorten_pages_or_skip_rows(busy_client, provider, prices):
    """Filtering after the query would leave ragged pages and a cursor that
    steps over rows, so it happens in the query."""
    from decimal import Decimal

    prices.quotes[("ethereum", NATIVE)] = Decimal("0.000001")  # everything is dust now
    busy_client.ctx.indexer._priced_at = None
    run(busy_client.ctx.indexer.run_once())

    payload = busy_client.get("/api/activity").json()
    assert payload["activities"] == [], "all of it is dust, so the page is honestly empty"
    assert busy_client.get("/api/activity", params={"dust": "show"}).json()["activities"]


def test_the_loading_indicator_is_not_a_stray_dot(client):
    """It used to sit beside the search box with nothing to attach it to."""
    body = client.get("/").text
    assert 'class="htmx-indicator spinner"' not in body
    assert 'hx-indicator="#topline"' in body

    css = pathlib.Path("app/web/static/app.css").read_text()
    assert ".topline.htmx-request .status__dot" in css


def test_the_open_wallet_address_can_be_copied(client):
    """Shortened for reading, whole on the clipboard: the address is the thing
    anyone actually needs to carry out of here."""
    cold = account_id(client, "Cold")
    body = client.get("/summary", params={"wallet": cold}).text

    button = re.search(r'<button[^>]*class="summary__addr"[^>]*>.*?</button>', body, re.S).group(0)
    full = re.search(r'data-address="(0x[0-9a-fA-F]{40})"', button).group(1)
    account = next(a for a in client.ctx.db.list_accounts() if a.id == cold)

    assert full.lower() == account.address, "the whole address, not the shortened one"
    assert "copyAddress(" in button
    assert "…" in button, "and the short form is what is displayed"


def test_the_summary_has_no_address_to_copy_when_nothing_is_selected(client):
    assert 'class="summary__addr"' not in client.get("/summary").text


def test_copying_says_so_when_the_clipboard_is_unavailable(client):
    """navigator.clipboard needs a secure context; over plain HTTP it is simply
    absent, and failing silently would look like a broken button."""
    behaviour = pathlib.Path("app/web/templates/_behaviour.html").read_text()
    assert "button.textContent = copied ? 'Copied' : address" in behaviour


def test_the_summary_header_has_no_nested_buttons(client):
    """A button inside a button is invalid HTML: the parser closes the outer one
    early, and everything after it lands outside — which is what threw the
    address onto its own line."""
    cold = account_id(client, "Cold")
    body = client.get("/summary", params={"wallet": cold}).text

    toggle = re.search(r'<button[^>]*class="summary__toggle".*?</button>', body, re.S).group(0)
    assert "<button" not in toggle[len("<button"):], "the toggle contains no other button"
    assert 'class="summary__addr"' in body, "the address button is a sibling"
    assert body.index('class="summary__toggle"') < body.index('class="summary__addr"')


def test_a_wallet_holding_one_known_asset_is_not_called_implausible(client, provider, prices):
    """Live data caught this: a wallet holding $495 of USDC and $0.54 of
    everything else had the USDC struck out, so the headline read $0.53. Holding
    almost entirely one stablecoin is ordinary; only assets nothing vouches for
    can be implausible."""
    from decimal import Decimal

    from app.models import AssetRef

    usdc = AssetRef("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC", 6)
    tiny = AssetRef("ethereum", "0x" + "1" * 40, "TINY", 18)
    for address in list(provider.token_balances):
        provider.token_balances[address] = []
    provider.token_balances[MAIN] = [Balance(usdc, 495_407_000), Balance(tiny, 10**18)]
    prices.quotes[("ethereum", tiny.contract_address)] = Decimal("2")
    client.ctx.indexer._priced_at = None
    run(client.ctx.indexer.run_once())

    payload = client.get("/api/portfolio").json()
    usdc_row = next(a for a in payload["assets"] if a["symbol"] == "USDC")

    assert payload["excluded_assets"] == [], "USDC is a token this chain is known for"
    assert usdc_row["value"] == "$495.41", "and it counts towards the total"


def test_a_scam_token_is_still_excluded(client, provider, prices):
    """The guard still does its job on what it was built for."""
    from decimal import Decimal

    from app.models import AssetRef

    scam = AssetRef("ethereum", "0x" + "9" * 40, "SCAM", 18)
    for address in list(provider.token_balances):
        provider.token_balances[address] = []
    provider.token_balances[MAIN] = [
        Balance(AssetRef("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC", 6), 10**6),
        Balance(scam, 10**17 * 10**18),
    ]
    prices.quotes[("ethereum", scam.contract_address)] = Decimal("0.049")
    client.ctx.indexer._priced_at = None
    run(client.ctx.indexer.run_once())

    assert client.get("/api/portfolio").json()["excluded_assets"] == ["SCAM"]


def test_the_sidebar_and_the_panel_agree_on_a_wallet(client):
    """Two different sums for one wallet — one in the list, one in the summary —
    is a contradiction the reader cannot resolve."""
    main = account_id(client, "Main")
    panel = client.get("/api/portfolio", params={"wallet": main}).json()["total"]
    sidebar = re.search(
        r'<div class="wallet[^"]*">.*?>Main</span>.*?title="([^"]+)"',
        client.get("/wallets").text,
        re.S,
    ).group(1)

    assert sidebar == panel


# --------------------------------------------------------------- impostors


def test_a_ticker_that_apes_a_known_one_is_never_trusted(client, provider, prices):
    """Live data: `Ụ᠋SDC` and `Ụ᠋5DT` — homoglyphs of USDC and USDT — were marked
    as *sent by the user*, because an ERC-20 contract can emit a Transfer log
    with any `from` it likes. Address poisoning does exactly that, so a forged
    ticker overrides every other signal."""
    from decimal import Decimal

    from app.models import AssetRef

    fake = AssetRef("ethereum", "0x" + "3" * 40, "Ụ᠋SDC", 6)
    provider.transfers = [
        *provider.transfers,
        transfer("poison", sender=MAIN, recipient=STRANGER, amount=10**9, asset=fake,
                 kind=TransferKind.TOKEN, log_index=21, block=21_000_095),
    ]
    for address in list(provider.token_balances):
        provider.token_balances[address] = []
    provider.token_balances[MAIN] = [Balance(fake, 10**9)]
    prices.quotes[("ethereum", fake.contract_address)] = Decimal("1")
    client.ctx.indexer._priced_at = None
    run(client.ctx.indexer.run_once())

    payload = client.get("/api/portfolio").json()
    visible = payload["assets"][: len(payload["assets"]) - payload["hidden_assets"]]

    assert "Ụ᠋SDC" not in [a["symbol"] for a in visible], (
        "priced, held and apparently sent — and still not shown"
    )
    # Still selectable, but under "Unrecognised" rather than beside the real
    # USDC: a monitor may not pretend an on-chain thing is not there.
    select = re.search(r"<select name=\"asset\".*?</select>", client.get("/").text, re.S).group(0)
    main_list, _, unrecognised = select.partition("<optgroup")
    assert "Ụ᠋SDC" not in main_list, "never listed beside the token it apes"
    assert "Ụ᠋SDC" in unrecognised, "but still selectable — it is on the chain"


def test_the_real_token_is_untouched(client):
    """The rule is about deception, not about the ticker: the genuine contract
    keeps its name."""
    body = client.get("/").text
    assert "USDC" in body


def test_unrecognised_tokens_stay_out_of_the_feed(client, provider):
    """An airdrop with no price, no listing and no history is what the noise on
    a live address is made of."""
    from app.models import AssetRef

    junk = AssetRef("ethereum", "0x" + "4" * 40, "Duhash.games", 18)
    provider.transfers = [
        *provider.transfers,
        transfer("junk", sender=STRANGER, recipient=MAIN, amount=150 * 10**18, asset=junk,
                 kind=TransferKind.TOKEN, log_index=31, block=21_000_096),
    ]
    run(client.ctx.indexer.run_once())

    symbols = [a["asset"]["symbol"] for a in client.get("/api/activity").json()["activities"]]
    assert "Duhash.games" not in symbols
    assert "Duhash.games" in [
        a["asset"]["symbol"]
        for a in client.get("/api/activity", params={"dust": "show"}).json()["activities"]
    ], "kept, indexed, and one click away"


def test_the_hidden_count_refreshes_with_the_filters(dusty_client):
    """It sits outside the swapped feed body, so it used to keep whatever it
    said when the page was first rendered — the toggle most visibly, but the
    count was also wrong after switching wallets."""
    note = dusty_client.get("/dust-note").text
    assert "2 noisy transfers hidden" in note
    assert 'hx-trigger="filtersApplied from:body"' in note

    shown = dusty_client.get("/dust-note", params={"dust": "show"}).text
    assert "Showing dust and unverified" in shown and "hidden" not in shown

    main = account_id(dusty_client, "Payments")
    assert "hidden" not in dusty_client.get("/dust-note", params={"wallet": main}).text

    behaviour = pathlib.Path("app/web/templates/_behaviour.html").read_text()
    assert "dispatchEvent(new Event('filtersApplied'))" in behaviour


# --------------------------------------------------------------- older history


def test_the_end_of_the_feed_offers_to_go_further(client):
    """The feed ends where the backfill window was set, not where the history
    does. An unexplained stop reads as "there is nothing older"."""
    body = client.get("/").text

    assert "Load" in body and "more days" in body
    assert 'hx-post="/history/older"' in body
    assert "indexed back to block #" in body


def test_asking_for_older_history_moves_the_floor_and_keeps_it(client):
    before = client.ctx.db.history_floor("ethereum")
    response = client.post("/history/older", headers={"sec-fetch-site": "same-origin"})

    assert response.status_code == 200
    after = client.ctx.db.get_sync_status("ethereum").requested_from_block
    assert after is not None and after < before

    # It survives a restart: BACKFILL_DAYS is a deployment default, this is the
    # reader having asked once and for all.
    client.post("/history/older", headers={"sec-fetch-site": "same-origin"})
    deeper = client.ctx.db.get_sync_status("ethereum").requested_from_block
    assert deeper < after

    client.ctx.db.request_history_from("ethereum", deeper + 10_000)
    assert client.ctx.db.get_sync_status("ethereum").requested_from_block == deeper, (
        "the request only ever goes deeper"
    )


def test_the_indexer_honours_a_deeper_request(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())
    provider.calls.clear()

    app_ctx.db.request_history_from("ethereum", 1_000_000)
    result = run(app_ctx.indexer.run_once())

    assert result.from_block == 1_000_000
    assert min(from_block for _, _, _, from_block, _ in provider.calls) == 1_000_000


def test_loading_older_history_is_not_open_to_other_sites(client):
    response = client.post("/history/older", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    assert client.ctx.db.get_sync_status("ethereum").requested_from_block is None


def test_small_but_ordinary_transfers_are_not_dust(dusty_client, provider, prices):
    """0.000346 ETH is 87 cents: below nobody's threshold for interesting, and
    not remotely spam. The two accusations are separate and it deserves neither."""
    provider.transfers = [
        *provider.transfers,
        transfer("small-eth", sender=STRANGER, recipient=MAIN, amount=346_000_000_000_000,
                 block=21_000_097),
    ]
    run(dusty_client.ctx.indexer.run_once())

    amounts = [a["amount"] for a in dusty_client.get("/api/activity").json()["activities"]]
    assert "0.000346" in amounts


def test_the_two_reasons_are_counted_apart(dusty_client, provider):
    """"Small" and "unknown" are different accusations, and lumping them into
    one number leaves the reader unable to tell which is which."""
    from app.models import AssetRef

    junk = AssetRef("ethereum", "0x" + "8" * 40, "Duhash.games", 18)
    provider.transfers = [
        *provider.transfers,
        transfer("junk2", sender=STRANGER, recipient=MAIN, amount=150 * 10**18, asset=junk,
                 kind=TransferKind.TOKEN, log_index=41, block=21_000_098),
    ]
    run(dusty_client.ctx.indexer.run_once())

    note = dusty_client.get("/dust-note").text
    assert "dust" in note and "unverified" in note


# ----------------------------------------------------------------- narrow


def test_the_scope_lives_in_the_bar_that_never_scrolls_away(client):
    """A sticky child stops sticking the moment its parent has scrolled past,
    so the summary panel cannot hold the scope on a phone. The top bar can."""
    body = client.get("/status").text

    assert "topline__scope" in body
    assert "openScopeSheet()" in body
    assert ">All activity</span>" in body

    # And it follows the selection rather than waiting for the next poll.
    assert "refreshWallets from:body" in body


def test_the_head_does_not_repeat_what_the_bar_already_says(client, mixed_client):
    """With a wallet selected the panel totals exactly what the feed shows, and
    the top bar has already named it. With "All activity" in the feed the panel
    is still totalling only your wallets, and has to say so."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    assert ".scope-is-panel .summary__scope { display: none; }" in css

    mine = next(a for a in mixed_client.ctx.db.list_accounts() if a.name == "Main")
    assert "scope-is-panel" in mixed_client.get("/summary", params={"wallet": mine.id}).text
    assert "scope-is-panel" not in mixed_client.get("/summary").text


def test_the_second_line_of_a_row_is_who(client):
    """Folded to two lines, a row says what moved and who with. The arrow
    repeats the direction so the counterparty carries it too."""
    body = client.get("/activity").text

    assert 'class="party__from"' in body
    assert "&larr;" in body or "&rarr;" in body


def test_one_wallet_in_scope_drops_its_name_from_every_row(client):
    """Repeated on every row, the name of the one wallet you selected is not
    information."""
    def layout_class(body: str) -> str:
        return re.search(r'<main class="([^"]*)"', body).group(1)

    account = client.ctx.db.list_accounts()[0]
    assert "one-wallet" in layout_class(client.get("/", params={"wallet": account.id}).text)
    assert "one-wallet" not in layout_class(client.get("/").text)

    css = pathlib.Path("app/web/static/app.css").read_text()
    assert ".one-wallet .cell-wallet { display: none; }" in css
    assert ".one-wallet .party__from { display: none; }" in css


def test_search_is_a_mode_on_a_phone_not_a_third_row(client):
    """Three stacked rows of controls above the feed is most of a phone screen
    spent on the thing used least."""
    body = client.get("/").text
    assert "toggleSearch(true)" in body and "closeSearch()" in body

    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]
    assert ".filters.is-searching .field--search" in narrow
    # A filter in force is never hidden behind a button.
    assert ".filters.has-search .field--search {" in narrow


def test_the_filter_strip_fits_the_phone_it_is_on(client):
    """A <select> is as wide as its widest option, and the unrecognised group's
    label is wider than a phone — which pushed the search button off screen."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]

    # Its width has to come from the row, not from its own contents.
    assert ".filters .field { flex: 1 1 0; min-width: 0; }" in narrow
    assert ".filters select { width: 100%; min-width: 0; }" in narrow


def test_a_deploy_that_changes_the_css_is_a_deploy_the_browser_notices(client):
    """A browser holding the old stylesheet and the new markup makes a shipped
    fix look like a shipped bug. The tag is a digest of the file, so it changes
    exactly when the file does and not on every request."""
    import re as _re

    from app.web.assets import static_url

    body = client.get("/").text
    hrefs = _re.findall(r'(?:href|src)="(/static/[^"]+)"', body)
    assert hrefs, "the page loads static assets"
    for href in hrefs:
        assert _re.search(r"\?v=[0-9a-f]{6,}$", href), href

    assert static_url("app.css") == static_url("app.css"), "stable between requests"
    assert static_url("app.css") != static_url("htmx.min.js")


def test_a_missing_static_file_does_not_take_the_page_down(client):
    from app.web.assets import static_url

    assert static_url("nope.css") == "/static/nope.css?v=0"


# ------------------------------------------------------- one tx, one entry


def _a_swap_feed(client):
    client.ctx.provider.transfers = [
        transfer("grp", sender=MAIN, recipient=STRANGER, amount=811 * 10**6,
                 asset=USDC, kind=TransferKind.TOKEN, log_index=3, block=21_000_500),
        transfer("grp", sender=MAIN, recipient=COLD, amount=376_866,
                 asset=USDC, kind=TransferKind.TOKEN, log_index=4, block=21_000_500),
    ]
    client.ctx.provider.head_block = 21_000_510
    run(client.ctx.indexer.run_once())
    return client.get("/activity").text


def test_a_transaction_is_one_entry_in_the_feed(client):
    """Two transfers of the same transaction, a second apart, read as two
    unrelated things — which is what the feed is for avoiding."""
    body = _a_swap_feed(client)

    assert body.count("row--folded") >= 1, "the second transfer is folded away"
    assert "row--more-of" in body, "and the entry says it is hiding something"


def test_the_transfer_carrying_the_value_leads(client):
    """It is the one the reader is looking for; leading with an incidental
    0.37 of change would bury the 811 that actually moved."""
    body = _a_swap_feed(client)
    lead = rows_of(body)[0]

    assert ">811<" in lead
    assert "0.376866" not in lead


def test_nothing_is_added_up_across_transfers(client):
    """A token added to an unrelated token is not a sum, and even two amounts
    of the same asset going to different places are two facts, not one."""
    body = _a_swap_feed(client)

    assert "811.376866" not in body
    assert "0.376866" in body, "the folded transfer is present, just not shown"


def test_the_folded_rows_are_already_in_the_page(client):
    """They came back with the same query. Fetching them again on a press would
    be a round trip for data the browser is already holding."""
    body = _a_swap_feed(client)
    folded = re.findall(r'<tr class="row row--[^"]*row--folded[^"]*"[^>]*data-tx="([^"]+)"', body)

    assert folded, "rendered, not deferred"
    assert 'onclick="toggleFold(this)"' in body
    assert "hx-get" not in re.search(r'<tr class="row row--more-of.*?</tr>', body, re.S).group(0)


def test_paging_is_not_disturbed_by_the_reordering(client):
    """Grouping moves rows within a transaction. Paging from a moved row would
    skip or repeat whatever sat between it and the true end of the page."""
    rows = client.ctx.db.query_activities(limit=500)
    assert len(rows) > 1

    body = client.get("/activity").text
    cursor = re.search(r'before=(\d+_\d+)', body)
    if cursor:
        timestamp, activity_id = (int(p) for p in cursor.group(1).split("_"))
        # The cursor is the smallest key on the page, whatever order it is shown in.
        shown = [int(i) for i in re.findall(r'data-activity="(\d+)"', body)]
        by_id = {r["id"]: r["block_timestamp"] for r in rows}
        assert (timestamp, activity_id) == min((by_id[i], i) for i in shown)


def test_a_folded_row_is_actually_hidden_on_a_phone(client):
    """`[data-activity]` is what makes a row a grid on a narrow screen, and an
    attribute selector outweighs a class: hiding one has to match it."""
    css = pathlib.Path("app/web/static/app.css").read_text()

    assert ".row--folded[data-activity] { display: none; }" in css

    # And restated after the rule that makes a row a grid: equal weight means
    # source order decides, so hidden has to come last.
    narrow = css.split("@media (max-width: 860px) {")[1]
    grid_at = narrow.index(".row[data-activity] {")
    hide_at = narrow.index(".row--folded[data-activity] { display: none; }")
    assert hide_at > grid_at
    assert ".row--folded[data-activity].is-shown { display: grid; }" in narrow


def test_the_foldout_stops_describing_rows_that_are_on_screen(client):
    """Expanded, "+ 3 more transfers" is captioning the three rows directly
    below it. All it still has to offer is the way back."""
    body = _a_swap_feed(client)
    assert 'class="foldout__one"' in body or 'class="foldout__count"' in body

    css = pathlib.Path("app/web/static/app.css").read_text()
    assert '.foldout[aria-expanded="true"] .foldout__count { display: none; }' in css
    assert '.foldout[aria-expanded="true"]::after { content: "hide"; }' in css


def test_where_and_when_are_context_not_findings(client):
    """As a grid of labelled rows, CHAIN / BLOCK / DEPTH / WHEN were the
    loudest thing on a panel that exists to show transfers."""
    row_id = client.ctx.db.query_activities(limit=1)[0]["id"]
    body = client.get(f"/tx/{row_id}").text

    assert "insp__where" in body and "insp__facts" not in body
    assert "<dt>Chain</dt>" not in body and "<dt>Block</dt>" not in body


def test_a_fee_the_chain_will_not_state_is_not_shown_as_zero(client):
    """A fee rendered as 0 is a claim about what something cost, and the wrong
    one. The fake provider has no receipts, so there is nothing to say."""
    row_id = client.ctx.db.query_activities(limit=1)[0]["id"]
    body = client.get(f"/tx/{row_id}").text

    assert "insp__cost" not in body
    assert "Fee" not in body


def test_a_provider_failure_costs_a_line_not_the_panel(client, monkeypatch):
    """This is the one call made because a person asked rather than on a timer,
    and it decorates a panel that is already useful without it."""
    async def boom(_tx_hash):
        raise RuntimeError("provider is having a moment")

    monkeypatch.setattr(client.ctx.provider, "get_transaction_cost", boom, raising=False)
    row_id = client.ctx.db.query_activities(limit=1)[0]["id"]
    response = client.get(f"/tx/{row_id}")

    assert response.status_code == 200
    assert "Transfers" in response.text
    assert "insp__cost" not in response.text


def test_a_fee_the_chain_does_state_is_priced_in_both(client):
    from app.models import TransactionCost

    row = client.ctx.db.query_activities(limit=1)[0]
    client.ctx.provider.costs[row["tx_hash"]] = TransactionCost(
        fee_raw=420_000_000_000_000, gas_used=41_382, gas_limit=52_000, succeeded=True
    )
    body = client.get(f"/tx/{row['id']}").text

    assert "0.00042 ETH" in body
    assert "41,382 / 52,000" in body
    assert "$" in body.split("Fee")[1].split("Gas")[0], "and what that was worth"


def test_a_failed_transaction_says_so(client):
    from app.models import TransactionCost

    row = client.ctx.db.query_activities(limit=1)[0]
    client.ctx.provider.costs[row["tx_hash"]] = TransactionCost(
        fee_raw=1, gas_used=21_000, gas_limit=21_000, succeeded=False
    )
    assert "failed" in client.get(f"/tx/{row['id']}").text


# --------------------------------------------------- the phone's budget


def test_the_portfolio_header_has_a_fixed_height_on_a_phone(client):
    """Its height must not depend on how many tokens have been sent to the
    address, which on this chain is not something the owner controls."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]

    assert ".holdings > .holding:not(.holding--compact)," in narrow
    assert ".holdings--all > .holding { display: block; }" in narrow, "the sheet shows all"


def test_the_native_balance_is_not_said_twice_on_a_phone(client):
    """It is a figure in the line directly above the list. A phone has the
    least room of any surface to spend repeating itself."""
    body = client.get("/summary").text
    compact = re.findall(r'class="holding([^"]*)"', body)
    native = [c for c in compact if "holding--native" in c]

    assert native, "the fixture holds the native asset"
    assert all("holding--compact" not in c for c in native)


def test_more_is_not_the_same_word_as_hidden(client):
    """"Hidden" is what the spam filter does. Folding the fourth token away for
    room is a different thing, and one word for both is how a reader stops
    trusting either."""
    body = client.get("/summary").text
    if "holdings__more" in body:
        more = re.search(r'class="holdings__more".*?</button>', body, re.S).group(0)
        assert "hidden" not in more
        assert "more" in more


def test_everything_held_is_a_tap_away(client):
    """Capping the panel at three is only honest if the rest are reachable."""
    body = client.get("/holdings").text
    shown = re.findall(r'class="holding__sym">([^<]+)<', body)
    panel = re.findall(r'class="holding__sym">([^<]+)<', client.get("/summary").text)

    assert len(shown) >= len(panel)
    assert "Assets" in body


def test_a_row_of_numbers_lines_up(client):
    """Each holding is its own grid, so an `auto` value column sizes every row
    differently and the amounts stop lining up."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]

    assert "minmax(0, 4.6em) minmax(0, 1fr) 6.6em" in narrow


def test_the_scope_class_is_kept_in_step_with_the_feed(client):
    """htmx swaps the feed and leaves the container's server-rendered class
    behind. A stale `one-wallet` hides a cell that spans two columns on
    transfers between your own wallets, and takes the row's layout with it."""
    behaviour = client.get("/").text

    assert "function syncScopeClass()" in behaviour
    assert "syncScopeClass();" in behaviour.split("function afterFilterChange")[1]


def test_a_transfer_between_your_wallets_spans_two_columns(client):
    """Which is exactly why hiding that cell cannot be left to a stale class."""
    body = client.get("/activity").text
    own = [row for row in rows_of(body) if "row--between" in row or "row--self" in row]

    assert own, "the fixture has a transfer between two owned wallets"
    assert 'colspan="2"' in own[0]


def test_everything_an_event_says_starts_on_one_vertical(client):
    """The direction badge was centred in a fixed box, so "IN" began seven
    pixels right of "OUT" and the second line wandered against the first."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]

    assert ".row[data-activity] .dir { min-width: 0; text-align: left; }" in narrow
    assert ".row--more-of td { padding: 0 0 4px 68px; }" in narrow


def test_the_hidden_count_is_not_explained_twice_on_a_phone(client):
    """181 = 111 + 70 is one fact explaining another, and the explanation
    crowds the thing it explains on the surface with the least room."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]
    assert ".dust-note__why { display: none; }" in narrow

    # ...and is still there on the screen that has room for it.
    body = client.get("/dust-note", params={"dust": ""}).text
    assert "dust-note__why" in body or "is-empty" in body


def test_a_disbelieved_asset_never_leads_the_compact_list(client, settings, provider, prices,
                                                          sample_transfers):
    """The phone shows two holdings. An asset excluded from the total is
    excluded because its quote is not credible — letting it take one of those
    two hands the top of the screen to the number just refused."""
    from decimal import Decimal

    from app.main import create_app
    from app.models import AssetRef, Balance

    scam = AssetRef("ethereum", "0x" + "5" * 40, "SCAM", 18)
    provider.transfers = sample_transfers
    provider.token_balances[MAIN] = [Balance(scam, 10**17 * 10**18)]
    prices.quotes[("ethereum", scam.contract_address)] = Decimal("0.049")

    app = create_app(settings=settings, config=make_config(), provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as scam_client:
        run(app.state.ctx.indexer.run_once())
        body = scam_client.get("/summary").text

    excluded = re.findall(r'class="holding[^"]*holding--excluded[^"]*"', body)
    assert excluded, "the fixture produces an excluded asset"
    assert all("holding--compact" not in c for c in excluded)
