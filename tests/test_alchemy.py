"""AlchemyProvider against realistic response payloads.

No API key is needed: httpx.MockTransport serves the JSON-RPC envelopes that
Alchemy documents, so the provider's own parsing, paging and retry code runs.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.chains.ethereum.alchemy import AlchemyProvider, ProviderError, build_url
from app.models import TransferKind
from tests.conftest import run

TX = "0x3847245c01829b043431067fb2bfa95f7b5bdc7e4246c843e7a573ab6f26f5ff"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WALLET = "0x28c6c06298d514db089934071355e5743bf21d60"


def make_provider(handler, **kwargs) -> AlchemyProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AlchemyProvider("https://eth-mainnet.g.alchemy.com/v2/test", client=client, **kwargs)


def rpc_result(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


EXTERNAL = {
    "blockNum": "0x1406f40",
    "uniqueId": f"{TX}:external",
    "hash": TX,
    "from": "0x1111111111111111111111111111111111111111",
    "to": WALLET,
    "value": 2.4,
    "asset": "ETH",
    "category": "external",
    "rawContract": {"value": "0x214e8348c4f00000", "address": None, "decimal": "0x12"},
    "metadata": {"blockTimestamp": "2024-10-09T09:42:11.000Z"},
}

ERC20 = {
    "blockNum": "0x1406f41",
    "uniqueId": f"{TX}:log:41",
    "hash": TX,
    "from": WALLET,
    "to": "0x2222222222222222222222222222222222222222",
    "value": 500.0,
    "asset": "USDC",
    "category": "erc20",
    "rawContract": {"value": "0x1dcd6500", "address": USDC, "decimal": "0x6"},
    "metadata": {"blockTimestamp": "2024-10-09T09:31:47.000Z"},
}

INTERNAL = {
    "blockNum": "0x1406f42",
    "uniqueId": f"{TX}:internal:0_1",
    "hash": TX,
    "from": "0x3333333333333333333333333333333333333333",
    "to": WALLET,
    "value": 0.05,
    "asset": "ETH",
    "category": "internal",
    "rawContract": {"value": "0xb1a2bc2ec50000", "address": None, "decimal": "0x12"},
    "metadata": {"blockTimestamp": "2024-10-09T08:17:02.000Z"},
}


def test_normalizes_each_category():
    def handler(request: httpx.Request) -> httpx.Response:
        return rpc_result({"transfers": [EXTERNAL, ERC20, INTERNAL]})

    provider = make_provider(handler)
    transfers = run(
        provider.get_transfers(WALLET, outgoing=False, from_block=0, to_block=21_000_000)
    )

    assert [t.kind for t in transfers] == [
        TransferKind.NATIVE,
        TransferKind.TOKEN,
        TransferKind.INTERNAL,
    ]
    assert [t.event_key for t in transfers] == [
        f"{TX}:external",
        f"{TX}:log:41",
        f"{TX}:internal:0_1",
    ]
    # Exact integer amounts, taken from rawContract, never from the lossy float.
    assert transfers[0].amount_raw == 2_400_000_000_000_000_000
    assert transfers[1].amount_raw == 500_000_000
    assert transfers[1].asset.decimals == 6
    assert transfers[1].asset.symbol == "USDC"
    assert transfers[0].asset.is_native
    assert transfers[0].block_number == 0x1406F40
    assert transfers[0].block_timestamp == 1728466931
    run(provider.close())


def test_float_value_is_never_used_for_the_amount():
    """A value too large for a float64 must still be exact."""
    huge = dict(EXTERNAL)
    huge["value"] = 1.2345678901234567e26
    huge["rawContract"] = {
        "value": hex(123456789012345678901234567),
        "address": None,
        "decimal": "0x12",
    }

    provider = make_provider(lambda request: rpc_result({"transfers": [huge]}))
    transfers = run(provider.get_transfers(WALLET, outgoing=True, from_block=0, to_block=1))

    assert transfers[0].amount_raw == 123456789012345678901234567
    run(provider.close())


def test_follows_page_keys():
    pages = [
        {"transfers": [EXTERNAL], "pageKey": "page-2"},
        {"transfers": [ERC20], "pageKey": "page-3"},
        {"transfers": [INTERNAL]},
    ]
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["params"][0])
        return rpc_result(pages[len(seen) - 1])

    provider = make_provider(handler)
    transfers = run(provider.get_transfers(WALLET, outgoing=False, from_block=10, to_block=20))

    assert len(transfers) == 3
    assert "pageKey" not in seen[0]
    assert seen[1]["pageKey"] == "page-2"
    assert seen[2]["pageKey"] == "page-3"
    run(provider.close())


def test_request_shape_matches_the_direction():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content)["params"][0])
        return rpc_result({"transfers": []})

    provider = make_provider(handler)
    run(provider.get_transfers(WALLET, outgoing=True, from_block=100, to_block=200))
    run(provider.get_transfers(WALLET, outgoing=False, from_block=100, to_block=200))

    assert captured[0]["fromAddress"] == WALLET and "toAddress" not in captured[0]
    assert captured[1]["toAddress"] == WALLET and "fromAddress" not in captured[1]
    assert captured[0]["fromBlock"] == "0x64" and captured[0]["toBlock"] == "0xc8"
    assert captured[0]["category"] == ["external", "internal", "erc20"]
    assert captured[0]["withMetadata"] is True
    run(provider.close())


def test_skips_transfers_that_cannot_be_represented():
    broken = [
        {**EXTERNAL, "uniqueId": None},                       # no identity
        {**EXTERNAL, "uniqueId": "a:external", "metadata": {}},  # no timestamp
        {**EXTERNAL, "uniqueId": "b:external", "rawContract": {}},  # no amount
        {**EXTERNAL, "uniqueId": "c:external", "category": "erc721"},  # not modelled
    ]
    provider = make_provider(lambda request: rpc_result({"transfers": broken}))
    assert run(provider.get_transfers(WALLET, outgoing=True, from_block=0, to_block=1)) == []
    run(provider.close())


def test_null_recipient_is_kept_as_an_empty_address():
    creation = {**EXTERNAL, "to": None, "uniqueId": f"{TX}:external"}
    provider = make_provider(lambda request: rpc_result({"transfers": [creation]}))
    transfers = run(provider.get_transfers(WALLET, outgoing=True, from_block=0, to_block=1))
    assert transfers[0].to_address == ""
    run(provider.close())


def test_token_metadata_fallback_is_cached():
    calls: list[str] = []
    no_meta = {**ERC20, "asset": None, "rawContract": {**ERC20["rawContract"], "decimal": None}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "alchemy_getTokenMetadata":
            return rpc_result({"symbol": "USDC", "decimals": 6, "name": "USD Coin"})
        return rpc_result({"transfers": [no_meta]})

    provider = make_provider(handler)
    first = run(provider.get_transfers(WALLET, outgoing=True, from_block=0, to_block=1))
    second = run(provider.get_transfers(WALLET, outgoing=True, from_block=0, to_block=1))

    assert first[0].asset.symbol == "USDC" and first[0].asset.decimals == 6
    assert second[0].asset.symbol == "USDC"
    assert calls.count("alchemy_getTokenMetadata") == 1, "metadata must be cached"
    run(provider.close())


def test_unknown_token_gets_a_readable_stub():
    no_meta = {**ERC20, "asset": None, "rawContract": {**ERC20["rawContract"], "decimal": None}}

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["method"] == "alchemy_getTokenMetadata":
            return rpc_result({"symbol": None, "decimals": None})
        return rpc_result({"transfers": [no_meta]})

    provider = make_provider(handler)
    transfers = run(provider.get_transfers(WALLET, outgoing=True, from_block=0, to_block=1))
    assert transfers[0].asset.symbol.startswith("0xa0b8")
    run(provider.close())


def test_balances_skip_zero_and_errored_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "alchemy_getTokenMetadata":
            return rpc_result({"symbol": "USDC", "decimals": 6})
        return rpc_result(
            {
                "address": WALLET,
                "tokenBalances": [
                    {"contractAddress": USDC, "tokenBalance": "0x1dcd6500"},
                    {"contractAddress": "0x" + "9" * 40, "tokenBalance": "0x0"},
                    {"contractAddress": "0x" + "8" * 40, "tokenBalance": None, "error": "boom"},
                ],
            }
        )

    provider = make_provider(handler)
    balances = run(provider.get_token_balances(WALLET))

    assert len(balances) == 1
    assert balances[0].amount_raw == 500_000_000
    assert balances[0].asset.symbol == "USDC"
    run(provider.close())


def test_head_block_and_native_balance():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        return rpc_result("0x1406f40" if method == "eth_blockNumber" else "0x429d069189e0000")

    provider = make_provider(handler)
    assert run(provider.get_head_block()) == 21_000_000
    assert run(provider.get_native_balance(WALLET)) == 300_000_000_000_000_000
    run(provider.close())


def test_rpc_error_becomes_a_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad request"}},
        )

    provider = make_provider(handler, max_retries=1)
    with pytest.raises(ProviderError, match="bad request"):
        run(provider.get_head_block())
    run(provider.close())


def test_retries_then_succeeds_on_rate_limit():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={"error": "slow down"})
        return rpc_result("0x1406f40")

    provider = make_provider(handler, max_retries=4)
    assert run(provider.get_head_block()) == 21_000_000
    assert attempts["n"] == 3
    run(provider.close())


def test_gives_up_after_max_retries():
    provider = make_provider(lambda request: httpx.Response(503), max_retries=2)
    with pytest.raises(ProviderError, match="failed after 2 attempts"):
        run(provider.get_head_block())
    run(provider.close())


def test_build_url():
    assert build_url("KEY") == "https://eth-mainnet.g.alchemy.com/v2/KEY"


def test_token_metadata_is_resolved_concurrently_and_capped():
    """An exchange address holds thousands of tokens and each needs a metadata
    lookup. Doing them one at a time never finishes; doing all of them wastes
    quota to display ten. So they run in parallel and the count is bounded."""
    pages = 0
    metadata_calls = 0
    in_flight = 0
    peak = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages, metadata_calls, in_flight, peak
        body = json.loads(request.content)
        if body["method"] == "alchemy_getTokenMetadata":
            metadata_calls += 1
            in_flight += 1
            peak = max(peak, in_flight)
            in_flight -= 1
            return rpc_result({"symbol": "TKN", "decimals": 18})
        pages += 1
        return rpc_result(
            {
                "address": WALLET,
                "tokenBalances": [
                    {"contractAddress": f"0x{pages:039x}{index:01x}", "tokenBalance": "0x1"}
                    for index in range(100)
                ],
                "pageKey": f"page-{pages}",
            }
        )

    source = make_provider(handler, max_token_lookups=150)
    balances = run(source.get_token_balances(WALLET))

    assert len(balances) == 150, "the cap is respected"
    assert metadata_calls == 150, "one lookup per held token, and no more"
    assert pages == 2, "paging stops once the cap is reached"
    run(source.close())


def test_the_same_token_is_looked_up_once_even_when_asked_at_once():
    """Two balances of the same token arriving together must not both fetch it."""
    metadata_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_calls
        body = json.loads(request.content)
        if body["method"] == "alchemy_getTokenMetadata":
            metadata_calls += 1
            return rpc_result({"symbol": "USDC", "decimals": 6})
        return rpc_result(
            {
                "address": WALLET,
                "tokenBalances": [
                    {"contractAddress": USDC, "tokenBalance": "0x1"},
                    {"contractAddress": USDC, "tokenBalance": "0x2"},
                ],
            }
        )

    source = make_provider(handler)
    run(source.get_token_balances(WALLET))

    assert metadata_calls == 1
    run(source.close())


def test_a_throttled_response_is_waited_out_not_given_up_on():
    """Alchemy answers a burst past the plan's compute-per-second ceiling with
    403, not 429. Reading that as "forbidden" abandons a call that would have
    succeeded a moment later — which is what took the live instance red."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(403)
        return rpc_result("0x1406f40")

    provider = make_provider(handler, max_retries=4)
    assert run(provider.get_head_block()) == 21_000_000
    assert attempts["n"] == 3
    run(provider.close())


def test_only_a_couple_of_requests_are_in_flight_at_once():
    """The free tier caps compute per second, and the backfill fans out over
    every wallet at once."""
    import asyncio as _asyncio

    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await _asyncio.sleep(0.01)
        in_flight -= 1
        return rpc_result("0x1406f40")

    async def exercise():
        provider = AlchemyProvider(
            "https://x/v2/k",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_concurrency=2,
        )
        await _asyncio.gather(*(provider.get_head_block() for _ in range(12)))
        await provider.close()

    run(exercise())
    assert peak <= 2
