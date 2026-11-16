"""Alchemy-backed :class:`~app.chains.ChainDataProvider` for Ethereum mainnet.

Chosen over an explorer API mainly because ``alchemy_getAssetTransfers`` returns
external, internal and ERC-20 transfers in one normalized shape *and* supplies a
``uniqueId`` per transfer -- a stable identity that distinguishes several
transfers inside the same transaction. See ARCHITECTURE.md for the trade-offs.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime

import httpx

from app.chains.ethereum.chain import ZERO_ADDRESS, EthereumChain
from app.chains.ethereum.known_assets import KNOWN_TOKENS, listed_decimals
from app.models import NATIVE, AssetRef, Balance, TransactionCost, Transfer, TransferKind
from app.redact import describe_failure, is_access_denied

log = logging.getLogger(__name__)

_MAX_COUNT = "0x3e8"  # 1000, the documented per-page maximum
_CATEGORIES = ["external", "internal", "erc20"]
# 403 belongs here: Alchemy answers a burst past the plan's compute-per-second
# ceiling with Forbidden rather than Too Many Requests, so treating it as a
# permission failure would give up on something that succeeds a second later.
_RETRY_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_THROTTLED = frozenset({403, 429})


class ProviderError(RuntimeError):
    """The upstream chain data provider could not satisfy a request."""


def _stub(contract_address: str) -> str:
    return f"{contract_address[:6]}…{contract_address[-4:]}"


def _valid_decimals(value: object) -> int | None:
    """Decimals an ERC-20 can actually have (a uint8), or ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255:
        return value
    return None


def _hex_to_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


class AlchemyProvider:
    chain_id = EthereumChain.chain_id
    name = "alchemy"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
        max_concurrency: int = 2,
        max_token_lookups: int = 200,
        metadata_retry_seconds: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._max_retries = max_retries
        self._max_token_lookups = max_token_lookups
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._request_id = 0
        # A successful lookup is kept for the life of the process: a token does
        # not change its decimals. A failed one only for a while — kept forever,
        # one bad minute at the provider used to fix a token's size at zero.
        self._token_metadata: dict[str, tuple[str, int]] = {}
        self._metadata_failures: dict[str, tuple[float, str]] = {}
        self._metadata_retry_seconds = metadata_retry_seconds
        self._token_metadata_locks: dict[str, asyncio.Lock] = {}
        self._native = AssetRef(
            chain_id=self.chain_id,
            contract_address=NATIVE,
            symbol=EthereumChain.native_symbol,
            decimals=EthereumChain.native_decimals,
        )

    # ------------------------------------------------------------------ rpc

    async def _rpc(self, method: str, params: list) -> object:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        delay = 0.5
        last_error = "no attempt made"

        for attempt in range(self._max_retries):
            try:
                async with self._semaphore:
                    response = await self._client.post(self._url, json=payload)
                if is_access_denied(response):
                    # Asking again changes nothing and spends the time a
                    # throttled caller would back off for.
                    raise ProviderError(
                        f"{method} failed: HTTP {response.status_code}"
                        " (the provider refused this key or app: check ALCHEMY_API_KEY"
                        " and the app's settings at the provider)"
                    )
                if response.status_code in _RETRY_STATUS:
                    last_error = f"HTTP {response.status_code}"
                    if response.status_code in _THROTTLED:
                        # Backing off by the usual step just walks into the same
                        # ceiling; a throttled caller has to actually wait.
                        delay = max(delay, 2.0)
                else:
                    response.raise_for_status()
                    body = response.json()
                    if error := body.get("error"):
                        raise ProviderError(
                            f"{method}: provider error {error.get('code')}: {error.get('message')}"
                        )
                    return body.get("result")
            except ProviderError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                _, last_error = describe_failure(exc)

            if attempt < self._max_retries - 1:
                # Jitter, so several stalled requests do not all resume together
                # and reproduce the burst that got them throttled.
                await asyncio.sleep(delay * (1 + random.random() * 0.4))
                delay *= 2

        raise ProviderError(f"{method} failed after {self._max_retries} attempts: {last_error}")

    # -------------------------------------------------------------- queries

    async def get_head_block(self) -> int:
        block = _hex_to_int(await self._rpc("eth_blockNumber", []))
        if block is None:
            raise ProviderError("eth_blockNumber returned no result")
        return block

    async def get_native_balance(self, address: str) -> int:
        balance = _hex_to_int(await self._rpc("eth_getBalance", [address, "latest"]))
        if balance is None:
            raise ProviderError(f"eth_getBalance returned no result for {address}")
        return balance

    async def get_transaction_cost(self, tx_hash: str) -> TransactionCost | None:
        """Fee, gas and outcome, from the receipt.

        Two calls, made only when someone opens a transaction: the receipt has
        what was spent, and the transaction itself has the limit that was set —
        which is the half that says whether the estimate was any good. A node
        that has pruned either simply returns null, and the caller shows
        nothing; a missing fee is not a zero fee.
        """
        receipt, transaction = await asyncio.gather(
            self._rpc("eth_getTransactionReceipt", [tx_hash]),
            self._rpc("eth_getTransactionByHash", [tx_hash]),
            return_exceptions=True,
        )
        if isinstance(receipt, Exception) or not isinstance(receipt, dict):
            return None

        gas_used = _hex_to_int(receipt.get("gasUsed"))
        price = _hex_to_int(receipt.get("effectiveGasPrice"))
        if gas_used is None or price is None:
            return None

        gas_limit = None
        if isinstance(transaction, dict):
            gas_limit = _hex_to_int(transaction.get("gas"))

        return TransactionCost(
            fee_raw=gas_used * price,
            gas_used=gas_used,
            gas_limit=gas_limit,
            # Pre-Byzantium receipts have no status field. Everything this app
            # indexes is far newer, and a missing field is not a failure.
            succeeded=_hex_to_int(receipt.get("status")) != 0,
        )

    async def get_token_balances(self, address: str) -> list[Balance]:
        """Non-zero token balances.

        Balances come back in pages of contract addresses; the symbol and
        decimals of each need a second call. Resolving those one at a time is
        fine for a personal wallet and hopeless for an exchange address, which
        can hold thousands — so they are resolved concurrently and the number of
        lookups is capped. A wallet past the cap is reported incompletely, which
        is why it is logged.
        """
        holdings: list[tuple[str, int]] = []
        page_key: str | None = None
        truncated = False

        while True:
            options: dict[str, object] = {"maxCount": 100}
            if page_key:
                options["pageKey"] = page_key
            result = await self._rpc("alchemy_getTokenBalances", [address, "erc20", options])
            if not isinstance(result, dict):
                break

            for entry in result.get("tokenBalances") or []:
                if entry.get("error"):
                    continue
                amount = _hex_to_int(entry.get("tokenBalance"))
                contract = (entry.get("contractAddress") or "").lower()
                if not amount or not contract:
                    continue
                if len(holdings) >= self._max_token_lookups:
                    truncated = True
                    break
                holdings.append((contract, amount))

            page_key = result.get("pageKey")
            if truncated or not page_key:
                break

        if truncated:
            log.warning(
                "%s holds more than %d tokens; the rest are not valued",
                address,
                self._max_token_lookups,
            )

        metadata = await asyncio.gather(*(self._token_meta(c) for c, _ in holdings))
        return [
            Balance(
                asset=AssetRef(self.chain_id, contract, symbol, decimals),
                amount_raw=amount,
            )
            for (contract, amount), (symbol, decimals) in zip(holdings, metadata, strict=True)
        ]

    async def get_transfers(
        self,
        address: str,
        *,
        outgoing: bool,
        from_block: int,
        to_block: int,
    ) -> list[Transfer]:
        if from_block > to_block:
            return []

        params: dict[str, object] = {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "category": _CATEGORIES,
            "withMetadata": True,
            "excludeZeroValue": True,
            "maxCount": _MAX_COUNT,
            "order": "asc",
            ("fromAddress" if outgoing else "toAddress"): address,
        }

        transfers: list[Transfer] = []
        page_key: str | None = None
        pages = 0

        while True:
            if page_key:
                params["pageKey"] = page_key
            result = await self._rpc("alchemy_getAssetTransfers", [params])
            if not isinstance(result, dict):
                break
            pages += 1

            for raw in result.get("transfers") or []:
                transfer = await self._normalize(raw)
                if transfer is not None:
                    transfers.append(transfer)

            page_key = result.get("pageKey")
            if not page_key:
                break
            if pages >= 1000:  # safety valve against a provider paging forever
                log.warning("stopped paging transfers for %s after %d pages", address, pages)
                break

        return transfers

    # ----------------------------------------------------------- normalize

    async def _normalize(self, raw: dict) -> Transfer | None:
        event_key = raw.get("uniqueId")
        tx_hash = raw.get("hash")
        block_number = _hex_to_int(raw.get("blockNum"))
        if not event_key or not tx_hash or block_number is None:
            return None

        category = raw.get("category")
        if category == "external":
            kind = TransferKind.NATIVE
        elif category == "internal":
            kind = TransferKind.INTERNAL
        elif category == "erc20":
            kind = TransferKind.TOKEN
        else:  # erc721 / erc1155 / specialnft are not requested and not modelled
            return None

        contract = raw.get("rawContract") or {}
        amount_raw = _hex_to_int(contract.get("value"))
        if amount_raw is None or amount_raw == 0:
            return None

        if kind is TransferKind.TOKEN:
            contract_address = (contract.get("address") or "").lower()
            if not contract_address:
                return None
            decimals = _valid_decimals(_hex_to_int(contract.get("decimal")))
            symbol = raw.get("asset")
            listed = listed_decimals(contract_address)
            if listed is not None:
                # The reviewed list wins over whatever this response says.
                decimals = listed
                symbol = symbol or KNOWN_TOKENS.get(contract_address)
            if decimals is None or not symbol:
                symbol, decimals = await self._token_meta(contract_address)
            asset = AssetRef(self.chain_id, contract_address, symbol, decimals)
        else:
            asset = self._native

        timestamp = _parse_timestamp((raw.get("metadata") or {}).get("blockTimestamp"))
        if timestamp is None:
            return None

        return Transfer(
            chain_id=self.chain_id,
            event_key=event_key,
            tx_hash=tx_hash.lower(),
            block_number=block_number,
            block_timestamp=timestamp,
            kind=kind,
            from_address=(raw.get("from") or ZERO_ADDRESS).lower(),
            to_address=(raw.get("to") or "").lower(),
            asset=asset,
            amount_raw=amount_raw,
        )

    async def _token_meta(self, contract_address: str) -> tuple[str, int | None]:
        """Symbol and decimals for a token; ``None`` decimals means unknown.

        Believed in this order: the vendored token list, which a person
        reviewed and which cannot have a bad minute; a lookup that succeeded;
        and only then the provider, asked again once a failure is older than
        ``metadata_retry_seconds``. Unknown is never reported as zero — zero
        decimals values a raw amount as whole tokens.
        """
        listed = listed_decimals(contract_address)
        if listed is not None:
            return KNOWN_TOKENS.get(contract_address) or _stub(contract_address), listed
        if cached := self._cached_meta(contract_address):
            return cached

        lock = self._token_metadata_locks.setdefault(contract_address, asyncio.Lock())
        async with lock:
            if cached := self._cached_meta(contract_address):
                return cached
            return await self._fetch_token_meta(contract_address)

    def _cached_meta(self, contract_address: str) -> tuple[str, int | None] | None:
        if known := self._token_metadata.get(contract_address):
            return known
        if failure := self._metadata_failures.get(contract_address):
            failed_at, symbol = failure
            if time.monotonic() - failed_at < self._metadata_retry_seconds:
                return symbol, None
        return None

    async def _fetch_token_meta(self, contract_address: str) -> tuple[str, int | None]:
        symbol, decimals = "", None
        try:
            result = await self._rpc("alchemy_getTokenMetadata", [contract_address])
        except ProviderError as exc:
            log.warning("token metadata lookup failed for %s: %s", contract_address, exc)
            result = None

        if isinstance(result, dict):
            symbol = (result.get("symbol") or "").strip()
            decimals = _valid_decimals(result.get("decimals"))

        # Unknown token: a stub rather than an empty column.
        symbol = symbol or _stub(contract_address)
        if decimals is None:
            self._metadata_failures[contract_address] = (time.monotonic(), symbol)
            return symbol, None

        self._metadata_failures.pop(contract_address, None)
        self._token_metadata[contract_address] = (symbol, decimals)
        return symbol, decimals

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_url(api_key: str, base: str = "https://eth-mainnet.g.alchemy.com/v2") -> str:
    return f"{base.rstrip('/')}/{api_key}"
