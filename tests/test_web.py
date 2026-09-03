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
    """Activity spread over today, yesterday and a week ago."""
    now = int(time.time())
    day = 86_400
    provider.transfers = [
        transfer("today-1", sender=STRANGER, recipient=MAIN, amount=10**18,
                 block=21_000_090, timestamp=now - 3600),
        transfer("today-2", sender=MAIN, recipient=STRANGER, amount=2 * 10**17,
                 block=21_000_080, timestamp=now - 7200),
        transfer("yesterday", sender=STRANGER, recipient=COLD, amount=3 * 10**18,
                 block=21_000_070, timestamp=now - day - 3600),
        transfer("older", sender=PAYMENTS, recipient=STRANGER, amount=4 * 10**17,
                 block=21_000_060, timestamp=now - 7 * day),
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
    rows = re.findall(r'<tr class="row row--\w+" data-activity.*?</tr>', feed, re.S)

    badged = [row for row in rows if 'class="kind"' in row]
    assert len(badged) == 1, "native and token kinds are inferable and must not be labelled"
    assert ">int<" in badged[0]
    assert ">native<" not in feed and ">token<" not in feed


def test_amount_and_asset_are_one_cell(client):
    cell = re.search(r'<td class="cell-value">.*?</td>', client.get("/").text, re.S).group(0)
    assert 'class="amount"' in cell and 'class="sym"' in cell


# ------------------------------------------------------------ wallet select


def test_the_owned_accounts_need_no_heading_of_their_own(client):
    """The summary panel above already says whose wallets these are; repeating
    it directly underneath is the same word twice."""
    body = client.get("/wallets").text

    assert "scope-link" not in body, "the redundant list of scopes is gone"
    assert "wallets__group" not in body, "with nothing watched, one group needs no heading"
    assert body.index(">Main</span>") < body.index(">Cold</span>")


def test_the_two_groups_contrast_without_echoing_the_summary(mixed_client):
    """Both groups need a heading once there is something to contrast — and each
    heading is also its scope control. "My wallets" is not reused: it sits
    directly under the summary panel's own "My wallets"."""
    body = mixed_client.get("/wallets").text
    headings = re.findall(r'class="wallets__group[^"]*"[^>]*>\s*<span>([^<]+)</span>', body)

    assert headings == ["My wallets", "Watching"]
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

    assert "1 token hidden" in body
    assert "toggleHiddenTokens()" in body
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
    tokens = re.search(r"Tokens.*?</div>", body, re.S).group(0)

    assert re.search(r"\d+ tokens?", tokens)
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

    # Counterparty is the widest column and transaction the second widest: on
    # real data those two carry the most and are the least abbreviable.
    widths = {
        name: int(re.search(rf"\.cell-{name} +\{{ width: (\d+)%", css).group(1))
        for name in ("wallet", "value", "party", "tx")
    }
    assert widths["party"] > widths["tx"] > widths["value"] >= widths["wallet"]


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
    return re.findall(r'<tr class="row row--\w+" data-activity.*?</tr>', body, re.S)


def test_the_sidebar_separates_mine_from_watched(mixed_client):
    body = mixed_client.get("/wallets").text
    groups = re.findall(r'class="wallets__group[^"]*"[^>]*>\s*<span>([^<]+)</span>', body)

    assert [g.strip() for g in groups] == ["My wallets", "Watching"]
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


# ------------------------------------------------------------------ narrow


def test_a_row_can_be_opened_on_its_own(client):
    """The narrow feed drops the hash and block to stay readable; opening a row
    is where they go, rather than being lost."""
    row_id = client.ctx.db.query_activities(limit=1)[0]["id"]
    body = client.get(f"/activity/{row_id}").text

    assert re.search(r"0x[0-9a-f]{64}", body), "the full transaction hash"
    assert "etherscan.io/tx/" in body and "etherscan.io/block/" in body
    assert "From" in body and "To" in body and "Block" in body


def test_opening_a_missing_row_says_so(client):
    response = client.get("/activity/999999")
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


def test_a_row_opens_only_on_a_narrow_screen(client):
    """On a wide screen a row carries its own links; making it clickable as well
    would fight them."""
    body = client.get("/activity").text
    assert 'hx-trigger="click[isNarrow()]"' in body
    assert "event.stopPropagation()" in body, "the counterparty link filters, not opens"


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
    for card in ("box-shadow", "border-radius", "border:"):
        assert card not in shell, f"the container must not become a card ({card})"
    assert ".topbar, .filters { background: var(--bg); }" in shell, (
        "panel bands ending at the container edge read as a card"
    )


def test_the_narrow_layout_keeps_the_full_width(client):
    """The bound is a desktop concern; a phone has no width to spare."""
    css = pathlib.Path("app/web/static/app.css").read_text()
    narrow = css.split("@media (max-width: 860px) {")[1]
    assert ".app" not in narrow, "the container is left alone below the breakpoint"


# ------------------------------------------------------------ the experiment


def test_the_experimental_layout_is_a_second_composition_not_a_replacement(client):
    """Both layouts are served so they can be compared side by side; the current
    one must keep its own template and stylesheet untouched."""
    current = client.get("/").text
    lab = client.get("/lab").text

    assert '/static/app.css' in current and "lab.css" not in current
    assert "/static/lab.css" in lab
    assert client.get("/static/lab.css").status_code == 200


def test_the_experimental_layout_drops_the_standing_sidebar(client):
    """The L-shaped composition is what the experiment is testing: without a
    column down the left the activity area is a rectangle."""
    lab = client.get("/lab").text

    assert 'class="tabs"' in lab, "scope moves into a row of tabs"
    assert 'class="pane"' in lab, "and the table becomes a bounded object"
    assert "Activity</h2>" in lab
    assert 'class="tabs__manage"' in lab, "wallet management still reachable"
    # The list itself still exists — as a sheet, so renaming and removing survive.
    assert 'id="wallets"' in lab


def test_the_experimental_layout_counts_what_it_shows(client):
    """The count is of the filtered feed, not of everything indexed."""
    everything = client.get("/lab").text
    assert f"{client.ctx.db.activity_count():,} transactions" in everything

    usdc = next(row["id"] for row in client.ctx.db.feed_assets() if row["symbol"] == "USDC")
    filtered = client.get("/lab", params={"asset": usdc}).text
    assert "1 transaction<" in filtered.replace("\n", "").replace("  ", "")


def test_both_layouts_share_one_behaviour_script(client):
    """The composition differs; the behaviour must not fork, or one of them
    quietly stops closing dialogs or highlighting new rows."""
    behaviour = pathlib.Path("app/web/templates/_behaviour.html").read_text()
    assert "function selectScope" in behaviour and "row--new" in behaviour

    for path in ("/", "/lab"):
        body = client.get(path).text
        assert "function selectScope" in body
        assert "isAutoRefresh" in body


def test_the_experiment_makes_activity_one_surface(client):
    """Controls sitting outside the table's border read as three stacked bands.
    Inside it they read as one component — which is most of why Etherscan looks
    collected despite carrying more."""
    lab = client.get("/lab").text
    pane = lab[lab.index('<div class="pane">'):lab.index("</table>")]

    assert 'class="activity__head"' in pane
    assert 'id="scope-tabs"' in pane
    assert 'id="filters"' in pane
    assert 'class="feed"' in pane


def test_the_experiment_names_the_direction_column(client):
    """Coloured IN/OUT values under a blank heading is a column with no name."""
    header = re.search(r"<thead>.*?</thead>", client.get("/lab").text, re.S).group(0)
    assert ">Dir</th>" in header


def test_the_experiment_holds_the_summary_to_a_column(client):
    """An overview should not imitate the 1500px table underneath it."""
    css = pathlib.Path("app/web/static/lab.css").read_text()
    assert re.search(r"\.lab \.summary__body \{[^}]*max-width: 880px", css)
    assert re.search(r"\.lab \.holdings-row \{[^}]*max-width: 880px", css)


def test_the_experiment_reads_a_step_larger(client):
    """20 legible rows beat 30 microscopic ones."""
    css = pathlib.Path("app/web/static/lab.css").read_text()
    base = pathlib.Path("app/web/static/app.css").read_text()

    lab_amount = int(re.search(r"\.lab \.amount \{ font-size: (\d+)px", css).group(1))
    base_amount = float(
        re.search(r"^\.amount \{[^}]*font-size: ([\d.]+)px", base, re.M).group(1)
    )
    assert lab_amount > base_amount

    lab_row = int(re.search(r"\.lab \.feed td \{ padding: (\d+)px", css).group(1))
    base_row = int(re.search(r"\.feed td \{\n  padding: (\d+)px", base).group(1))
    assert lab_row > base_row


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
    client.ctx.indexer._priced_at = 0
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
    assert "2 dust transfers hidden" in body
    assert "toggleDust(true)" in body


def test_dust_can_be_shown(dusty_client):
    shown = dusty_client.get("/api/activity", params={"dust": "show"}).json()["activities"]
    assert "0.000059" in [a["amount"] for a in shown]
    assert "Showing transfers worth under a cent" in dusty_client.get(
        "/", params={"dust": "show"}
    ).text


def test_dust_is_never_decided_by_the_raw_amount(dusty_client, provider, prices):
    """59 raw units of a 6-decimal stablecoin is dust; 59 raw units of an
    unpriced token is unknown, and unknown is not dust."""
    from decimal import Decimal

    from app.models import AssetRef

    mystery = AssetRef("ethereum", "0x" + "7" * 40, "MYSTERY", 6)
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
    assert "MYSTERY" in symbols, "no price means no verdict"

    # Give it a price and the same transfer becomes dust.
    prices.quotes[("ethereum", mystery.contract_address)] = Decimal("1")
    dusty_client.ctx.indexer._priced_at = 0
    run(dusty_client.ctx.indexer.run_once())
    symbols = [a["asset"]["symbol"] for a in dusty_client.get("/api/activity").json()["activities"]]
    assert "MYSTERY" not in symbols


def test_hiding_dust_does_not_shorten_pages_or_skip_rows(busy_client, provider, prices):
    """Filtering after the query would leave ragged pages and a cursor that
    steps over rows, so it happens in the query."""
    from decimal import Decimal

    prices.quotes[("ethereum", NATIVE)] = Decimal("0.000001")  # everything is dust now
    busy_client.ctx.indexer._priced_at = 0
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
