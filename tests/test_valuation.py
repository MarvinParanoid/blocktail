"""An honest portfolio total (audit findings D-03, D-04, U-05).

Three ways the total used to lie, each reproduced here against the real code
path — provider, cache, database, valuation, page:

* a token the app itself had called a forgery and hidden was still summed in;
* a metadata lookup that failed once was cached as ``decimals = 0`` for the
  life of the process, and 1 WBTC was then worth six trillion dollars;
* the guard against an absurd quote protected the total, but the same quote
  was still printed on feed rows and in the holdings sheet.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chains.ethereum.alchemy import AlchemyProvider
from app.main import create_app
from app.models import NATIVE, AssetRef, Balance, TransferKind
from tests.conftest import (
    COLD,
    MAIN,
    STRANGER,
    USDC,
    FakePrices,
    FakeProvider,
    make_config,
    make_settings,
    run,
    transfer,
)

WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"  # in the vendored list, 8 decimals
OBSCURE = "0x00000000000000000000000000000000000c0ffe"  # in no list
FORGED_USDT = AssetRef("ethereum", "0x1111111111111111111111111111111111111111", "USDT", 6)
NMT = AssetRef("ethereum", "0x2222222222222222222222222222222222222222", "NMT", 18)

ETH_PRICE = Decimal(3000)


def app_for(tmp_path, provider, quotes, **config):
    app = create_app(
        settings=make_settings(tmp_path),
        config=make_config(**config),
        provider=provider,
        price_source=FakePrices(quotes={("ethereum", k): v for k, v in quotes.items()}),
        run_indexer=False,
    )
    return app


def sidebar_values(client) -> dict[str, str]:
    """Wallet name -> the exact value in the sidebar row's tooltip."""
    html = client.get("/wallets").text
    return dict(
        re.findall(
            r'class="wallet__name">([^<]+)</span>\s*'
            r'<span class="wallet__value num" title="([^"]+)"',
            html,
        )
    )


def account_id(client, name: str) -> int:
    accounts = client.get("/api/accounts").json()["accounts"]
    return next(a["id"] for a in accounts if a["name"] == name)


# ---------------------------------------------------------- 1, 6: hidden


@pytest.fixture
def forged(tmp_path):
    provider = FakeProvider(
        native_balances={MAIN: 10**18},
        token_balances={MAIN: [Balance(USDC, 1000 * 10**6), Balance(FORGED_USDT, 5000 * 10**6)]},
    )
    app = app_for(
        tmp_path, provider,
        {NATIVE: ETH_PRICE, USDC.contract_address: Decimal(1),
         FORGED_USDT.contract_address: Decimal(1)},
        wallets=[{"name": "Main", "address": MAIN}],
    )
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        yield client


def test_a_hidden_impostor_is_not_in_the_total(forged):
    portfolio = forged.get("/api/portfolio").json()
    assert portfolio["total"] == "$4,000"
    assert portfolio["hidden_assets"] == 1


def test_the_sidebar_agrees_when_an_impostor_is_held(forged):
    assert sidebar_values(forged)["Main"] == "$4,000"


def test_showing_hidden_tokens_does_not_change_the_total(forged):
    """"Show" reveals the hidden rows; it is not a different valuation."""
    plain = forged.get("/summary").text
    revealed = forged.get("/summary", params={"dust": "show"}).text
    assert "holding--hidden" in plain, "the impostor is rendered, folded away"
    for html in (plain, revealed):
        assert "$4,000" in html
        assert "$9,000" not in html
    assert forged.get("/api/portfolio", params={"dust": "show"}).json()["total"] == "$4,000"


# ------------------------------------------------- 5: sidebar == portfolio


def test_sidebar_and_portfolio_are_the_same_valuation(tmp_path):
    """A token that is most of one small wallet but a sliver of the whole is
    judged once, for the whole: otherwise the wallets do not add up to the
    total above them."""
    other = AssetRef("ethereum", "0x3333333333333333333333333333333333333333", "OTHER", 18)
    provider = FakeProvider(
        native_balances={MAIN: 33 * 10**18, COLD: 10**17},
        token_balances={COLD: [Balance(other, 9500 * 10**18)]},
    )
    app = app_for(
        tmp_path, provider,
        {NATIVE: ETH_PRICE, other.contract_address: Decimal(1)},
        wallets=[{"name": "Main", "address": MAIN}, {"name": "Cold", "address": COLD}],
    )
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        total = client.get("/api/portfolio").json()["total"]
        sidebar = sidebar_values(client)

        def dollars(text):
            return Decimal(text.replace("$", "").replace(",", ""))

        assert dollars(total) == dollars(sidebar["Main"]) + dollars(sidebar["Cold"])
        for name in ("Main", "Cold"):
            single = client.get("/api/portfolio", params={"wallet": account_id(client, name)})
            assert single.json()["total"] == sidebar[name]


# ------------------------------------------------- 2, 3, 4: decimals path


class Chain:
    """A scripted Alchemy behind httpx.MockTransport."""

    def __init__(self, holdings: dict[str, int]):
        self.holdings = holdings
        self.metadata_down = True
        self.metadata_calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]

        def ok(result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        if method == "eth_blockNumber":
            return ok(hex(21_000_100))
        if method == "alchemy_getAssetTransfers":
            return ok({"transfers": []})
        if method == "eth_getBalance":
            return ok(hex(10**18 if params[0] == MAIN else 0))
        if method == "alchemy_getTokenBalances":
            balances = self.holdings if params[0] == MAIN else {}
            return ok({"tokenBalances": [
                {"contractAddress": c, "tokenBalance": hex(v)} for c, v in balances.items()
            ]})
        if method == "alchemy_getTokenMetadata":
            self.metadata_calls.append(params[0])
            if self.metadata_down:
                return httpx.Response(503)
            return ok({"symbol": "COFFEE", "decimals": 6, "name": "Coffee"})
        raise AssertionError(method)


def chain_app(tmp_path, chain: Chain, **provider_kwargs):
    provider_kwargs.setdefault("max_retries", 1)
    provider = AlchemyProvider(
        "https://eth-mainnet.g.alchemy.com/v2/test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(chain)),
        **provider_kwargs,
    )
    return app_for(
        tmp_path, provider,
        {NATIVE: ETH_PRICE, WBTC: Decimal(60000), OBSCURE: Decimal(1)},
        wallets=[{"name": "Main", "address": MAIN}],
        trusted_assets=[OBSCURE],  # vouched for, so only its decimals are in doubt
    )


def test_listed_token_decimals_come_from_the_vendored_list(tmp_path):
    chain = Chain({WBTC: 10**8})  # exactly 1 WBTC
    app = chain_app(tmp_path, chain)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        portfolio = client.get("/api/portfolio").json()
        assert portfolio["total"] == "$63,000"
        wbtc = next(a for a in portfolio["assets"] if a["symbol"] == "WBTC")
        assert wbtc["amount"] == "1" and wbtc["value"] == "$60,000"
        assert WBTC not in chain.metadata_calls, "a listed token needs no lookup"


def test_a_failed_lookup_is_unknown_not_zero(tmp_path):
    chain = Chain({OBSCURE: 2_500_000})  # 2.5 COFFEE at 6 decimals
    app = chain_app(tmp_path, chain)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        portfolio = client.get("/api/portfolio").json()
        assert portfolio["total"] == "$3,000", "an asset of unknown size is not valued"
        coffee = next(a for a in portfolio["assets"] if not a["native"])
        assert coffee["value"] is None
        assert portfolio["unpriced_assets"] >= 1

        row = app.state.ctx.db.connect().execute(
            "SELECT decimals, decimals_known FROM assets WHERE contract_address = ?", (OBSCURE,)
        ).fetchone()
        assert row["decimals_known"] == 0


def test_metadata_is_retried_once_the_provider_recovers(tmp_path):
    chain = Chain({OBSCURE: 5_000_000})  # 5 COFFEE at 6 decimals
    app = chain_app(tmp_path, chain, metadata_retry_seconds=0)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        assert client.get("/api/portfolio").json()["total"] == "$3,000"

        chain.metadata_down = False
        run(app.state.ctx.indexer.run_once())
        portfolio = client.get("/api/portfolio").json()
        assert portfolio["total"] == "$3,005"
        coffee = next(a for a in portfolio["assets"] if not a["native"])
        assert (coffee["symbol"], coffee["amount"]) == ("COFFEE", "5")


def test_a_failure_is_not_retried_on_every_call():
    """Not cached forever, and not re-asked on every balance read either."""
    chain = Chain({OBSCURE: 1})
    provider = AlchemyProvider(
        "https://x/v2/k", client=httpx.AsyncClient(transport=httpx.MockTransport(chain)),
        max_retries=1, metadata_retry_seconds=3600,
    )
    run(provider.get_token_balances(MAIN))
    chain.metadata_down = False
    second = run(provider.get_token_balances(MAIN))
    run(provider.close())
    assert len(chain.metadata_calls) == 1
    assert second[0].asset.decimals is None


def seed_wrong_decimals(app, contract: str, symbol: str):
    """What an older version left behind after a failed lookup: a zero, in a
    database that has not yet seen the migration that distrusts zeros."""
    db = app.state.ctx.db
    db.migrate()
    conn = db.connect()
    conn.execute(
        "INSERT INTO assets (chain_id, contract_address, symbol, decimals) VALUES (?, ?, ?, 0)",
        ("ethereum", contract, symbol),
    )
    conn.execute("ALTER TABLE assets DROP COLUMN decimals_known")
    conn.execute("DELETE FROM schema_migrations WHERE version = 7")


@pytest.mark.parametrize("contract", [WBTC, OBSCURE])
def test_a_restart_does_not_keep_wrong_decimals(tmp_path, contract):
    holdings = {WBTC: 10**8} if contract == WBTC else {OBSCURE: 2_500_000}
    chain = Chain(holdings)

    first = chain_app(tmp_path, chain)
    seed_wrong_decimals(first, contract, f"{contract[:6]}…{contract[-4:]}")
    first.state.ctx.db.close()

    # Restart with the provider still failing: the old zero must not come back
    # as a valuation, and a listed token is corrected from the list at once.
    app = chain_app(tmp_path, chain)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        total = client.get("/api/portfolio").json()["total"]
        assert total == ("$63,000" if contract == WBTC else "$3,000")
        assert "000,000,000" not in client.get("/").text


# ---------------------------------------------------- 7: excluded quotes


@pytest.fixture
def scam(tmp_path):
    provider = FakeProvider(
        native_balances={MAIN: 10**18},
        token_balances={MAIN: [Balance(USDC, 1000 * 10**6), Balance(NMT, 10**12 * 10**18)]},
        transfers=[
            transfer("nmt-airdrop", sender=STRANGER, recipient=MAIN, amount=10**12 * 10**18,
                     asset=NMT, kind=TransferKind.TOKEN, log_index=1, block=21_000_050),
        ],
    )
    app = app_for(
        tmp_path, provider,
        {NATIVE: ETH_PRICE, USDC.contract_address: Decimal(1), NMT.contract_address: Decimal(420)},
        wallets=[{"name": "Main", "address": MAIN}],
    )
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        yield client


ABSURD = "$420,000,000,000,000"


def test_an_excluded_asset_is_named_and_left_out(scam):
    portfolio = scam.get("/api/portfolio").json()
    assert portfolio["total"] == "$4,000"
    assert portfolio["excluded_assets"] == ["NMT"]


def test_an_excluded_asset_shows_no_value_in_the_feed(scam):
    activity = scam.get("/api/activity", params={"dust": "show"}).json()["activities"]
    nmt = next(a for a in activity if a["asset"]["symbol"] == "NMT")
    assert nmt["value"] is None
    for path in ("/", "/activity", "/activity?dust=show"):
        assert ABSURD not in scam.get(path).text


def test_an_excluded_asset_shows_no_value_in_the_holdings(scam):
    sheet = scam.get("/holdings").text
    assert ABSURD not in sheet
    item = re.search(r'<li class="([^"]*)">(?:(?!</li>).)*?>NMT<', sheet, re.DOTALL)
    assert item and "holding--excluded" in item.group(1)
    assert ABSURD not in scam.get("/summary").text
    nmt = next(a for a in scam.get("/api/portfolio").json()["assets"] if a["symbol"] == "NMT")
    assert nmt["value"] is None


def test_an_excluded_asset_sorts_after_the_credible_ones(scam):
    symbols = [a["symbol"] for a in scam.get("/api/portfolio").json()["assets"]]
    assert symbols.index("NMT") > symbols.index("USDC")


def test_a_feed_row_of_unknown_size_is_not_valued(tmp_path):
    unknown = AssetRef("ethereum", OBSCURE, "0x0000…0ffe", None)
    provider = FakeProvider(transfers=[
        transfer("mystery", sender=STRANGER, recipient=MAIN, amount=10**12, asset=unknown,
                 kind=TransferKind.TOKEN, log_index=0, block=21_000_060),
    ])
    app = app_for(tmp_path, provider, {NATIVE: ETH_PRICE, OBSCURE: Decimal(1)},
                  wallets=[{"name": "Main", "address": MAIN}], trusted_assets=[OBSCURE])
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        row = next(a for a in client.get("/api/activity").json()["activities"]
                   if a["asset"]["symbol"].startswith("0x0000"))
        assert row["value"] is None
        assert "$1,000,000,000,000" not in client.get("/").text


# ------------------------------------------------------ database boundary


def test_known_decimals_are_never_downgraded(tmp_path):
    app = app_for(tmp_path, FakeProvider(), {NATIVE: ETH_PRICE})
    db = app.state.ctx.db
    db.migrate()
    good = db.get_or_create_asset(AssetRef("ethereum", OBSCURE, "COFFEE", 6))
    again = db.get_or_create_asset(AssetRef("ethereum", OBSCURE, "0x0000…0ffe", None))
    row = db.connect().execute("SELECT symbol, decimals, decimals_known FROM assets WHERE id = ?",
                               (good,)).fetchone()
    assert good == again
    assert (row["symbol"], row["decimals"], row["decimals_known"]) == ("COFFEE", 6, 1)
    db.close()


def test_an_excluded_asset_shows_no_value_in_the_inspector(scam):
    activity = scam.get("/api/activity", params={"dust": "show"}).json()["activities"]
    nmt = next(a for a in activity if a["asset"]["symbol"] == "NMT")
    inspector = scam.get(f"/tx/{nmt['id']}")
    assert inspector.status_code == 200
    assert "NMT" in inspector.text
    assert ABSURD not in inspector.text


def test_a_forged_tickers_quote_is_never_printed(forged):
    """Hidden is not enough: "Show" would reveal the forger's own pool price."""
    nmt_free = forged.get("/summary").text + forged.get("/holdings").text
    assert "$5,000" not in nmt_free
    usdt = [a for a in forged.get("/api/portfolio").json()["assets"] if a["symbol"] == "USDT"]
    assert usdt and all(a["value"] is None for a in usdt)


def test_a_forged_tickers_transfer_is_not_valued_in_the_feed(tmp_path):
    # Held as well as received: only held assets are priced, and an unpriced
    # row would pass this test for the wrong reason.
    provider = FakeProvider(token_balances={MAIN: [Balance(FORGED_USDT, 5000 * 10**6)]}, transfers=[
        transfer("forged-in", sender=STRANGER, recipient=MAIN, amount=5000 * 10**6,
                 asset=FORGED_USDT, kind=TransferKind.TOKEN, log_index=0, block=21_000_070),
    ])
    app = app_for(tmp_path, provider, {NATIVE: ETH_PRICE, FORGED_USDT.contract_address: Decimal(1)},
                  wallets=[{"name": "Main", "address": MAIN}])
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        rows = client.get("/api/activity", params={"dust": "show"}).json()["activities"]
        forged_row = next(a for a in rows if a["asset"]["symbol"] == "USDT")
        assert forged_row["value"] is None
        assert "$5,000" not in client.get("/activity", params={"dust": "show"}).text


def test_a_large_total_is_not_rounded_by_the_decimal_context(tmp_path):
    """The old sum added an absurd quote and took it away again in a 28-digit
    context, and the dollars below it were lost."""
    provider = FakeProvider(
        native_balances={MAIN: 10**18},
        token_balances={MAIN: [Balance(USDC, 12_543_849 * 10**6), Balance(NMT, 10**27 * 10**18)]},
    )
    app = app_for(
        tmp_path, provider,
        {NATIVE: ETH_PRICE, USDC.contract_address: Decimal(1), NMT.contract_address: Decimal(420)},
        wallets=[{"name": "Main", "address": MAIN}],
    )
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        assert client.get("/api/portfolio").json()["total"] == "$12,546,849"


def test_an_excluded_asset_is_listed_even_past_the_cut(tmp_path):
    """Sorting it last must not push it off a capped list: the note above
    names it, so the list has to show it."""
    many = [AssetRef("ethereum", f"0x{i:040x}", f"T{i}", 18) for i in range(1, 6)]
    provider = FakeProvider(
        native_balances={MAIN: 10**18},
        token_balances={MAIN: [Balance(a, 10**18) for a in many] + [Balance(NMT, 10**30)]},
    )
    app = create_app(
        settings=make_settings(tmp_path, max_token_balances=2),
        config=make_config(wallets=[{"name": "Main", "address": MAIN}],
                           trusted_assets=[a.contract_address for a in many]),
        provider=provider,
        price_source=FakePrices(quotes={
            ("ethereum", NATIVE): ETH_PRICE, ("ethereum", NMT.contract_address): Decimal(420),
            **{("ethereum", a.contract_address): Decimal(2) for a in many},
        }),
        run_indexer=False,
    )
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        symbols = [a["symbol"] for a in client.get("/api/portfolio").json()["assets"]]
        assert "NMT" in symbols and symbols[-1] == "NMT"
        assert "excluded" in client.get("/holdings").text
