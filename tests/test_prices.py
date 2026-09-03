"""AlchemyPrices against mocked HTTP, and the rules valuation must obey."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.models import NATIVE, AssetRef
from app.prices import AlchemyPrices, PriceError, PriceSource
from tests.conftest import run

ETH = AssetRef("ethereum", NATIVE, "ETH", 18)
USDC = AssetRef("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC", 6)
DAI = AssetRef("ethereum", "0x6b175474e89094c44da98b954eedeac495271d0f", "DAI", 18)


def make(handler, **kwargs) -> AlchemyPrices:
    return AlchemyPrices(
        "key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
    )


def quote(value: str) -> list[dict]:
    return [{"currency": "USD", "value": value, "lastUpdatedAt": "2026-09-03T11:00:00Z"}]


def default_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("by-symbol"):
        return httpx.Response(200, json={"data": [{"symbol": "ETH", "prices": quote("3120.55")}]})
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "data": [
                {"network": a["network"], "address": a["address"], "prices": quote("1.0")}
                for a in body["addresses"]
            ]
        },
    )


def test_prices_native_and_tokens():
    source = make(default_handler)
    prices = run(source.get_prices([ETH, USDC, DAI]))

    assert prices[("ethereum", NATIVE)] == Decimal("3120.55")
    assert prices[("ethereum", USDC.contract_address)] == Decimal("1.0")
    assert len(prices) == 3
    run(source.close())


def test_uses_the_documented_request_shapes():
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        seen.append((request.method, str(request.url), payload))
        return default_handler(request)

    source = make(handler)
    run(source.get_prices([ETH, USDC]))

    by_symbol = next(call for call in seen if "by-symbol" in call[1])
    by_address = next(call for call in seen if "by-address" in call[1])

    assert by_symbol[0] == "GET" and "symbols=ETH" in by_symbol[1]
    assert by_address[0] == "POST"
    assert by_address[2] == {
        "addresses": [{"network": "eth-mainnet", "address": USDC.contract_address}]
    }
    assert "/prices/v1/key/" in by_symbol[1], "the key goes in the path"
    run(source.close())


def test_batches_addresses_to_the_documented_limit():
    batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("by-symbol"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        batches.append(len(body["addresses"]))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"network": a["network"], "address": a["address"], "prices": quote("2")}
                    for a in body["addresses"]
                ]
            },
        )

    tokens = [
        AssetRef("ethereum", f"0x{index:040x}", f"T{index}", 18) for index in range(1, 61)
    ]
    source = make(handler)
    prices = run(source.get_prices(tokens))

    assert batches == [25, 25, 10]
    assert len(prices) == 60
    run(source.close())


def test_an_asset_the_source_cannot_value_is_absent_not_zero():
    """A missing price must never become a zero valuation."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("by-symbol"):
            return httpx.Response(200, json={"data": [{"symbol": "ETH", "prices": quote("3000")}]})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"network": "eth-mainnet", "address": USDC.contract_address, "prices": quote("1")},
                    {"network": "eth-mainnet", "address": DAI.contract_address,
                     "prices": [], "error": "no liquidity"},
                ]
            },
        )

    source = make(handler)
    prices = run(source.get_prices([ETH, USDC, DAI]))

    assert ("ethereum", DAI.contract_address) not in prices
    assert ("ethereum", USDC.contract_address) in prices
    run(source.close())


def test_zero_and_unparseable_quotes_are_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("by-symbol"):
            return httpx.Response(200, json={"data": [{"symbol": "ETH", "prices": quote("0")}]})
        return httpx.Response(
            200,
            json={"data": [{"network": "eth-mainnet", "address": USDC.contract_address,
                            "prices": quote("not-a-number")}]},
        )

    source = make(handler)
    assert run(source.get_prices([ETH, USDC])) == {}
    run(source.close())


def test_a_token_failure_does_not_lose_the_native_price():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("by-symbol"):
            return httpx.Response(200, json={"data": [{"symbol": "ETH", "prices": quote("3000")}]})
        return httpx.Response(500)

    source = make(handler, max_retries=1)
    prices = run(source.get_prices([ETH, USDC]))

    assert prices == {("ethereum", NATIVE): Decimal("3000")}
    run(source.close())


def test_total_failure_raises():
    source = make(lambda request: httpx.Response(503), max_retries=1)
    with pytest.raises(PriceError):
        run(source.get_prices([ETH, USDC]))
    run(source.close())


def test_prices_use_decimal_not_float():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("by-symbol"):
            return httpx.Response(
                200, json={"data": [{"symbol": "ETH", "prices": quote("3120.123456789012345678")}]}
            )
        return httpx.Response(200, json={"data": []})

    source = make(handler)
    price = run(source.get_prices([ETH]))[("ethereum", NATIVE)]

    assert isinstance(price, Decimal)
    assert str(price) == "3120.123456789012345678"
    run(source.close())


def test_satisfies_the_protocol():
    assert isinstance(AlchemyPrices("k"), PriceSource)
