"""Asset valuation.

Prices are the one thing in blocktail that is not on-chain data, so they sit
behind their own small seam rather than being bolted onto ``ChainDataProvider``.
Everything downstream treats a missing price as a fact to report, never as zero.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

import httpx

from app.models import AssetRef

log = logging.getLogger(__name__)

USD = "USD"
_BATCH = 25  # documented maximum addresses per by-address request
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Alchemy's own name for each chain, kept next to the Alchemy client that needs
# it rather than in the chain-neutral layer.
_NETWORK_SLUGS = {"ethereum": "eth-mainnet"}


class PriceError(RuntimeError):
    pass


@runtime_checkable
class PriceSource(Protocol):
    """Spot prices for assets, in a single fiat currency."""

    currency: str

    async def get_prices(self, assets: list[AssetRef]) -> dict[tuple[str, str], Decimal]:
        """Map ``(chain_id, contract_address)`` to a price. Assets the source
        cannot value are simply absent from the result."""

    async def close(self) -> None: ...


def _to_decimal(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


class AlchemyPrices:
    """Alchemy Prices API. Uses the same key as the data provider."""

    currency = USD
    name = "alchemy-prices"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.g.alchemy.com/prices/v1",
        timeout: float = 20.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/{api_key}"
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        delay = 0.5
        last: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await self._client.request(method, f"{self._base}{path}", **kwargs)
                if response.status_code in _RETRY_STATUS:
                    last = PriceError(f"prices {path}: HTTP {response.status_code}")
                else:
                    response.raise_for_status()
                    return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
            if attempt < self._max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
        raise PriceError(f"prices {path} failed after {self._max_retries} attempts: {last}")

    async def get_prices(self, assets: list[AssetRef]) -> dict[tuple[str, str], Decimal]:
        native = [a for a in assets if a.is_native]
        tokens = [a for a in assets if not a.is_native]

        results = await asyncio.gather(
            self._native_prices(native),
            self._token_prices(tokens),
            return_exceptions=True,
        )

        prices: dict[tuple[str, str], Decimal] = {}
        errors = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(result)
            else:
                prices.update(result)

        if errors and not prices:
            raise PriceError("; ".join(str(e) for e in errors))
        for error in errors:
            log.warning("partial price failure: %s", error)
        return prices

    async def _native_prices(self, assets: list[AssetRef]) -> dict[tuple[str, str], Decimal]:
        if not assets:
            return {}
        symbols = sorted({asset.symbol for asset in assets})
        body = await self._request(
            "GET", "/tokens/by-symbol", params=[("symbols", s) for s in symbols]
        )

        by_symbol: dict[str, Decimal] = {}
        for entry in body.get("data") or []:
            price = self._first_usd(entry)
            if price is not None and entry.get("symbol"):
                by_symbol[entry["symbol"].upper()] = price

        return {
            (asset.chain_id, asset.contract_address): by_symbol[asset.symbol.upper()]
            for asset in assets
            if asset.symbol.upper() in by_symbol
        }

    async def _token_prices(self, assets: list[AssetRef]) -> dict[tuple[str, str], Decimal]:
        wanted = [asset for asset in assets if asset.chain_id in _NETWORK_SLUGS]
        prices: dict[tuple[str, str], Decimal] = {}

        for start in range(0, len(wanted), _BATCH):
            batch = wanted[start : start + _BATCH]
            body = await self._request(
                "POST",
                "/tokens/by-address",
                json={
                    "addresses": [
                        {
                            "network": _NETWORK_SLUGS[asset.chain_id],
                            "address": asset.contract_address,
                        }
                        for asset in batch
                    ]
                },
            )
            slug_to_chain = {slug: chain for chain, slug in _NETWORK_SLUGS.items()}
            for entry in body.get("data") or []:
                price = self._first_usd(entry)
                chain = slug_to_chain.get(entry.get("network", ""))
                address = (entry.get("address") or "").lower()
                if price is not None and chain and address:
                    prices[(chain, address)] = price

        return prices

    @staticmethod
    def _first_usd(entry: dict) -> Decimal | None:
        if entry.get("error"):
            return None
        for quote in entry.get("prices") or []:
            if (quote.get("currency") or "").upper() == USD:
                return _to_decimal(quote.get("value"))
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
