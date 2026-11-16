"""Search (S7.1: audit U-09 and LIKE escaping).

What the search box promises: whatever a row shows — its hash, its
addresses, its token, the name of a wallet, a label from the config, or a
built-in name such as "Uniswap: Universal Router" — finds that row, as plain
text. Nothing typed is a pattern: `%` is a percent sign, `_` an underscore,
`\\` a backslash. And nothing typed is SQL.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import (
    BINANCE,
    COLD,
    MAIN,
    PAYMENTS,
    STRANGER,
    address,
    make_config,
    run,
    transfer,
    tx_hash,
)

ROUTER = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD"     # built in: Uniswap: Universal Router
ROUTER_2 = "0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af"   # built in, and labelled in the config
DESK = address("desk")
TEMP = address("temp")
SAME_SITE = {"sec-fetch-site": "same-origin"}

SEEDS = ["to-router", "to-router-2", "to-binance", "from-desk", "ops", "fund", "plain",
         "temp-in"]


@pytest.fixture
def client(settings, provider, prices):
    config = make_config(
        wallets=[
            {"name": "Main", "address": MAIN},
            {"name": "Ops_1", "address": COLD},
            {"name": "Fund 100%", "address": PAYMENTS},
        ],
        labels={BINANCE: "Binance", DESK: "Desk\\East", ROUTER_2: "My swapper"},
    )
    provider.transfers = [
        transfer("to-router", sender=MAIN, recipient=ROUTER, amount=10**17, block=21_000_001),
        transfer("to-router-2", sender=MAIN, recipient=ROUTER_2, amount=10**17,
                 block=21_000_002),
        transfer("to-binance", sender=MAIN, recipient=BINANCE, amount=10**17, block=21_000_003),
        transfer("from-desk", sender=DESK, recipient=MAIN, amount=10**17, block=21_000_004),
        transfer("ops", sender=COLD, recipient=address("x1"), amount=10**17, block=21_000_005),
        transfer("fund", sender=PAYMENTS, recipient=address("x2"), amount=10**17,
                 block=21_000_006),
        transfer("plain", sender=STRANGER, recipient=MAIN, amount=10**17, block=21_000_007),
        transfer("temp-in", sender=address("x3"), recipient=TEMP, amount=10**17,
                 block=21_000_008),
    ]
    app = create_app(settings=settings, config=config, provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as test_client:
        run(app.state.ctx.indexer.run_once())
        test_client.ctx = app.state.ctx
        yield test_client


def found(client, q, **params) -> set[str]:
    """The seeds of the rows /activity returns for this search."""
    body = client.get("/activity", params={"q": q, **params}).text
    hashes = set(re.findall(r'data-tx="(0x[0-9a-f]{64})"', body))
    return {seed for seed in SEEDS if tx_hash(seed) in hashes}


def account(client, name):
    return next(a.id for a in client.ctx.db.list_accounts() if a.name == name)


EVERYTHING = {"to-router", "to-router-2", "to-binance", "from-desk", "ops", "fund", "plain"}


# ------------------------------------------------------------------ U-09


@pytest.mark.parametrize("q", ["universal router", "Uniswap", "UNISWAP: UNIVERSAL", "router"])
def test_a_built_in_name_finds_its_rows(client, q):
    assert found(client, q) >= {"to-router"}


def test_a_built_in_name_finds_only_its_rows(client):
    assert found(client, "universal router 2") == {"to-router-2"}


def test_a_config_label_still_wins_the_display_and_is_found(client):
    """An address can have both: the config's label is what the row shows,
    and the built-in name is still what it is."""
    assert found(client, "my swapper") == {"to-router-2"}
    assert "to-router-2" in found(client, "universal router")


def test_config_labels_wallet_names_addresses_and_hashes_still_work(client):
    assert found(client, "binance") == {"to-binance"}
    assert found(client, "ops") == {"ops"}
    assert found(client, BINANCE.lower()[2:14]) == {"to-binance"}
    assert found(client, BINANCE.upper()[2:14]) == {"to-binance"}, "case does not matter"
    assert found(client, tx_hash("plain")) == {"plain"}
    assert found(client, "ETH") == EVERYTHING, "the token's symbol"


def test_a_name_that_matches_nothing_finds_nothing(client):
    assert found(client, "definitely-not-there") == set()


@pytest.mark.parametrize("q", ["", "   "])
def test_an_empty_search_is_no_search(client, q):
    assert found(client, q) == EVERYTHING


# ----------------------------------------------------------- LIKE escaping


@pytest.mark.parametrize("q, expected", [
    ("%", {"fund"}),            # the one name with a percent sign
    ("100%", {"fund"}),
    ("_", {"ops"}),             # the one name with an underscore
    ("s_1", {"ops"}),
    ("p_1", set()),             # "_" is not "any character": "Ops_1" has no "p_1"
    ("o%1", set()),             # "%" is not "anything"
    ("%_", set()),
    ("\\", {"from-desk"}),      # the escape character itself, as text
    ("k\\E", {"from-desk"}),
    ("\\%", set()),             # backslash then percent: no name has that
    ("\\_", set()),
])
def test_wildcards_and_the_escape_character_are_plain_text(client, q, expected):
    assert found(client, q) == expected


def test_like_pattern_escapes_and_wraps():
    from app.db import like_pattern

    assert like_pattern("abc") == "%abc%"
    assert like_pattern("100%") == "%100\\%%"
    assert like_pattern("a_b") == "%a\\_b%"
    assert like_pattern("a\\b") == "%a\\\\b%"
    assert like_pattern("\\%_") == "%\\\\\\%\\_%"


@pytest.mark.parametrize("q", [
    "' OR 1=1 --",
    "%' OR '1'='1",
    "x'); DROP TABLE activities; --",
    '" OR ""="',
])
def test_sql_in_the_box_is_text(client, q):
    assert found(client, q) == set()
    assert found(client, "") == EVERYTHING, "the table is still there"


# ------------------------------------------------------- with other filters


def test_search_within_one_wallet(client):
    assert found(client, "universal", wallet=account(client, "Main")) == {
        "to-router", "to-router-2"}
    assert found(client, "universal", wallet=account(client, "Ops_1")) == set()


def test_search_with_direction_and_scope(client):
    assert found(client, "universal", direction="out") == {"to-router", "to-router-2"}
    assert found(client, "universal", direction="in") == set()
    assert found(client, "%", scope="mine") == {"fund"}


def test_the_counts_follow_the_same_search(client):
    """The hidden-transfers note counts with the same filters as the feed."""
    from app.web.routes import _counts, _parse_filters, _scope_ids

    ctx = client.ctx
    for q, rows in [("universal router", 2), ("_", 1), ("%", 1), ("\\", 1), ("", 7)]:
        filters = _parse_filters(None, None, None, q, None, None, "show")
        assert _counts(ctx, filters, _scope_ids(ctx, filters))["total"] == rows, q


def test_a_removed_wallet_is_not_found_by_name_or_counterparty(client):
    client.post("/wallets", data={"name": "Temp_x", "address": TEMP, "chain": "ethereum"},
                headers=SAME_SITE)
    run(client.ctx.indexer.run_once())
    assert found(client, "temp_x") == {"temp-in"}
    client.post(f"/wallets/{account(client, 'Temp_x')}/remove", headers=SAME_SITE)
    assert found(client, "temp_x") == set()
    assert found(client, address("x3")) == set(), "its counterparty finds nothing either"
    assert found(client, "") == EVERYTHING
