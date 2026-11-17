"""Offline lab for what blocktail asks of Alchemy (S10a audit).

Nothing here touches the network or the application's behaviour. The real
AlchemyProvider and AlchemyPrices run against FakeAlchemy, an httpx
MockTransport that answers Alchemy's JSON-RPC and Prices API from synthetic
data, so every HTTP request the application would send is counted — pages,
retries and cache misses included — not merely the high-level calls. The
indexer runs on a virtual clock: its cadences (balances, prices) and the
provider's metadata retry window read `time.monotonic` from this lab, and
retry back-off sleeps are recorded instead of slept.

CountingProvider is the same counter for tests that use the FakeProvider.

Reports never carry addresses or keys: wallets appear as W1…W7.

    python -m tests.quota_lab          the scenario tables used in
                                       docs/audit/alchemy-quota.md
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.chains.ethereum.alchemy as alchemy_module  # noqa: E402
import app.prices as prices_module  # noqa: E402
import app.sync as sync_module  # noqa: E402
from app.chains.ethereum.alchemy import AlchemyProvider  # noqa: E402
from app.main import build_context  # noqa: E402
from app.prices import AlchemyPrices  # noqa: E402
from tests.conftest import address, make_config, make_settings, tx_hash  # noqa: E402

# --------------------------------------------------------------- CU costs
#
# https://www.alchemy.com/docs/reference/compute-unit-costs, read 2026-11-17
# (the page shows no last-updated date). Costs are per request; the page
# gives no per-page, per-result or per-parameter variation for these methods.
# Its "Compute unit cost for error codes" table: HTTP 429 and 403 cost 0;
# other 4xx/5xx cost the endpoint's own value. Free plan (alchemy.com/pricing,
# same day): 30M CU per month, 300 CU/s (15 requests/s) over a rolling 10 s.
CU = {
    "eth_blockNumber": 10,
    "eth_getBalance": 20,
    "eth_getTransactionReceipt": 20,
    "eth_getTransactionByHash": 20,
    "alchemy_getAssetTransfers": 120,
    "alchemy_getTokenBalances": 20,
    "alchemy_getTokenMetadata": 10,
    "prices:/tokens/by-symbol": 40,
    "prices:/tokens/by-address": 40,
}
FREE_PLAN_CU_PER_MONTH = 30_000_000
CU_SOURCE = "alchemy.com/docs/reference/compute-unit-costs and alchemy.com/pricing, read 2026-11-17"

# What production runs with (/root/blocktail-build/.env, read 2026-11-17; the
# rest are the defaults in app/config.py).
PRODUCTION = dict(sync_interval=90, backfill_days=90, reorg_depth=64,
                  price_refresh=300, balance_refresh=600, full_scan_interval=0)
# S10b.1: the same, with the two-level sync on (FULL_SCAN_INTERVAL_SECONDS).
# PRODUCTION above stays what S10a audited: every wallet scanned every cycle.
TWO_LEVEL = {**PRODUCTION, "full_scan_interval": 900}
WALLETS = 7
BLOCK_SECONDS = 12


def cu_of(record: dict) -> int:
    """What Alchemy charges for one request: 0 for 429/403, the method's cost otherwise."""
    if record["status"] in (429, 403):
        return 0
    return CU[record["method"]]


# ------------------------------------------------------------ virtual time


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float, *args) -> None:
        self.slept.append(seconds)       # the back-off a retry would wait
        self.now += max(0.0, seconds)    # virtual time passes; the real loop does not wait
        await _real_sleep(0)


_real_sleep = asyncio.sleep


class _Proxy(SimpleNamespace):
    """A module stand-in: its own attributes first, the real module's after."""

    def __init__(self, real, **override) -> None:
        super().__init__(**override)
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


def install_clock(clock: Clock):
    """Point the indexer's, the provider's and the price client's notion of
    time at the clock. Returns a function that undoes it. The event loop's own
    clock is untouched."""
    import time as real_time

    saved = (sync_module.time, alchemy_module.time, alchemy_module.asyncio, prices_module.asyncio)
    timer = _Proxy(real_time, monotonic=clock.monotonic)
    sync_module.time = timer
    alchemy_module.time = timer
    alchemy_module.asyncio = _Proxy(asyncio, sleep=clock.sleep)
    prices_module.asyncio = _Proxy(asyncio, sleep=clock.sleep)

    def undo() -> None:
        (sync_module.time, alchemy_module.time, alchemy_module.asyncio,
         prices_module.asyncio) = saved
    return undo


# ------------------------------------------------------------- the server


@dataclass
class Holding:
    contract: str
    symbol: str
    decimals: int | None      # None: the metadata lookup answers without decimals
    amount: int = 10**18


@dataclass
class FakeAlchemy:
    """Alchemy as seen through HTTP: JSON-RPC on one URL, the Prices API on another."""

    wallets: dict[str, str]                       # address -> label (W1…)
    head: int = 21_000_000
    transfers: list[dict] = field(default_factory=list)
    holdings: dict[str, list[Holding]] = field(default_factory=dict)
    ether: dict[str, int] = field(default_factory=dict)     # address -> wei; 1 ETH if unset
    page_size: int | None = None                  # None: maxCount, as Alchemy honours it
    # (method, status) or (method, status, "W1 transfers-out"): each used once,
    # by the next request of that method — or of that wallet and reason.
    failures: list[tuple] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    broken: dict[str, int] = field(default_factory=dict)    # method -> status, every time
    clock: object | None = None                             # stamps each request when set
    _attempts: Counter = field(default_factory=Counter)

    # -- recording

    def _record(self, method: str, reason: str, wallet: str | None, status: int, key: str) -> None:
        self._attempts[key] += 1
        self.records.append({
            "method": method, "reason": reason, "wallet": wallet, "status": status,
            "ok": status == 200, "attempt": self._attempts[key],
            "t": self.clock.now if self.clock is not None else None,
        })

    def _fail(self, method: str, target: str | None = None) -> int | None:
        if method in self.broken:
            return self.broken[method]
        for i, failure in enumerate(self.failures):
            wanted, status, only = (*failure, None)[:3]
            if wanted == method and (only is None or only == target):
                del self.failures[i]
                return status
        return None

    def take(self) -> list[dict]:
        out, self.records = self.records, []
        return out

    # -- transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if "/tokens/" in path:
            return self._prices(request, path)
        body = json.loads(request.content)
        method, params, rid = body["method"], body["params"], body["id"]
        reason, wallet = self._why(method, params)
        key = f"rpc:{rid}"
        if (status := self._fail(method, f"{wallet} {reason}")) is not None:
            self._record(method, reason, wallet, status, key)
            return httpx.Response(status, json={"error": "injected"})
        self._record(method, reason, wallet, 200, key)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid,
                                         "result": self._answer(method, params)})

    def _why(self, method: str, params: list) -> tuple[str, str | None]:
        if method == "eth_blockNumber":
            return "head", None
        if method == "alchemy_getAssetTransfers":
            p = params[0]
            side = "out" if "fromAddress" in p else "in"
            who = p.get("fromAddress") or p.get("toAddress")
            return f"transfers-{side}", self.wallets.get(who)
        if method in ("eth_getBalance", "alchemy_getTokenBalances"):
            return "balances", self.wallets.get(params[0])
        if method == "alchemy_getTokenMetadata":
            return "token-metadata", None
        if method in ("eth_getTransactionReceipt", "eth_getTransactionByHash"):
            return "ui-transaction-cost", None
        return "other", None

    def _answer(self, method: str, params: list):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBalance":
            return hex(self.ether.get(params[0], 10**18))
        if method == "alchemy_getTokenBalances":
            who, _, options = params
            held = self.holdings.get(who, [])
            size = options.get("maxCount", 100)
            start = int(options.get("pageKey") or 0)
            page = held[start:start + size]
            nxt = start + size
            return {"address": who, "pageKey": str(nxt) if nxt < len(held) else None,
                    "tokenBalances": [{"contractAddress": h.contract,
                                       "tokenBalance": hex(h.amount)} for h in page]}
        if method == "alchemy_getTokenMetadata":
            contract = params[0]
            for held in self.holdings.values():
                for h in held:
                    if h.contract == contract:
                        return {"symbol": h.symbol, "decimals": h.decimals}
            return {"symbol": None, "decimals": None}
        if method == "alchemy_getAssetTransfers":
            return self._transfers(params[0])
        if method == "eth_getTransactionReceipt":
            return {"gasUsed": hex(21000), "effectiveGasPrice": hex(10**9), "status": "0x1"}
        if method == "eth_getTransactionByHash":
            return {"gas": hex(21000)}
        raise AssertionError(method)

    def _transfers(self, p: dict) -> dict:
        lo, hi = int(p["fromBlock"], 16), int(p["toBlock"], 16)
        side, who = (("from", p["fromAddress"]) if "fromAddress" in p else ("to", p["toAddress"]))
        matching = [t for t in self.transfers
                    if lo <= int(t["blockNum"], 16) <= hi and t[side] == who]
        size = self.page_size or int(p["maxCount"], 16)
        start = int(p.get("pageKey") or 0)
        nxt = start + size
        return {"transfers": matching[start:start + size],
                "pageKey": str(nxt) if nxt < len(matching) else None}

    def _prices(self, request: httpx.Request, path: str) -> httpx.Response:
        endpoint = "/tokens/" + path.split("/tokens/", 1)[1]
        method = f"prices:{endpoint}"
        key = f"prices:{id(request)}:{endpoint}"
        if (status := self._fail(method)) is not None:
            self._record(method, "prices", None, status, key)
            return httpx.Response(status, json={})
        self._record(method, "prices", None, 200, key)
        if endpoint == "/tokens/by-symbol":
            data = [{"symbol": s, "prices": [{"currency": "usd", "value": "3000"}]}
                    for s in request.url.params.get_list("symbols")]
        else:
            data = [{"network": a["network"], "address": a["address"],
                     "prices": [{"currency": "usd", "value": "1"}]}
                    for a in json.loads(request.content)["addresses"]]
        return httpx.Response(200, json={"data": data})

    # -- events

    def add_transfer(self, *, sender: str, recipient: str, block: int, token: Holding | None = None,
                     amount: int = 10**17, seed: str | None = None, category: str | None = None,
                     move_balances: bool = True) -> dict:
        """A transfer on chain; by default it moves the balances it would move."""
        seed = seed or f"t{len(self.transfers)}"
        raw: dict = {
            "uniqueId": f"{tx_hash(seed)}:{'log:0' if token else 'external'}",
            "hash": tx_hash(seed), "blockNum": hex(block),
            "category": category or ("erc20" if token else "external"),
            "from": sender, "to": recipient,
            "asset": token.symbol if token else "ETH",
            "rawContract": {"value": hex(amount),
                            "address": token.contract if token else None,
                            "decimal": hex(token.decimals) if token and token.decimals is not None
                            else (None if token else "0x12")},
            "metadata": {"blockTimestamp": datetime.fromtimestamp(
                1_700_000_000 + block * BLOCK_SECONDS, timezone.utc).isoformat()},
        }
        self.transfers.append(raw)
        if move_balances:
            self._move(sender, recipient, token, amount)
        return raw

    def remove_transfer(self, raw: dict, *, move_balances: bool = True) -> None:
        """A reorg took it out of the chain."""
        self.transfers.remove(raw)
        if move_balances:
            token = None
            if raw["category"] == "erc20":
                token = self._holding(raw["rawContract"]["address"], raw["asset"])
            self._move(raw["to"], raw["from"], token, int(raw["rawContract"]["value"], 16))

    def _holding(self, contract: str, symbol: str) -> Holding:
        for held in self.holdings.values():
            for h in held:
                if h.contract == contract:
                    return h
        return Holding(contract, symbol, 18, 0)

    def _move(self, sender: str, recipient: str, token: Holding | None, amount: int) -> None:
        if token is None:
            if sender in self.wallets:
                self.ether[sender] = self.ether.get(sender, 10**18) - amount
            if recipient in self.wallets:
                self.ether[recipient] = self.ether.get(recipient, 10**18) + amount
            return
        for who, sign in ((sender, -1), (recipient, 1)):
            if who not in self.wallets:
                continue
            held = self.holdings.setdefault(who, [])
            mine = next((h for h in held if h.contract == token.contract), None)
            if mine is None:
                mine = Holding(token.contract, token.symbol, token.decimals, 0)
                held.append(mine)
            mine.amount += sign * amount
            if mine.amount <= 0:
                held.remove(mine)


# --------------------------------------------------------------- the lab


def synthetic_wallets(n: int = WALLETS) -> list[str]:
    return [address(f"quota-wallet-{i}") for i in range(n)]


TOKEN_POOL = 13   # distinct tokens held across production's wallets (14 held assets less ETH)


def synthetic_holdings(wallets: list[str], per_wallet=(6, 7, 7, 2, 3, 1, 1)) -> dict:
    """Tokens each wallet holds — production's own counts (balance rows minus
    ETH) drawn from a pool of production's number of distinct tokens, read
    2026-11-17. Synthetic contracts, none on the vendored token list."""
    pool = [Holding(address(f"quota-token-{k}"), f"T{k}", 18) for k in range(TOKEN_POOL)]
    out = {}
    offset = 0
    for w, count in zip(wallets, per_wallet, strict=False):
        # The same contracts, each wallet its own amount.
        out[w] = [Holding(h.contract, h.symbol, h.decimals, h.amount)
                  for h in (pool[(offset + k) % TOKEN_POOL] for k in range(count))]
        offset += count
    return out


class Lab:
    """The indexer, the real provider and price client, FakeAlchemy, a clock."""

    def __init__(self, tmp: Path, *, wallets: list[str] | None = None, server: FakeAlchemy | None = None,
                 settings: dict | None = None) -> None:
        self.tmp = tmp
        self.wallets = wallets or synthetic_wallets()
        self.server = server or FakeAlchemy(
            wallets={w: f"W{i + 1}" for i, w in enumerate(self.wallets)},
            holdings=synthetic_holdings(self.wallets))
        self.params = {**PRODUCTION, **(settings or {})}
        self.clock = Clock()
        self.server.clock = self.clock
        self.undo = install_clock(self.clock)
        # One loop for the lab's life: the provider's semaphore and client
        # outlive a cycle, as they do in the running application.
        self.loop = asyncio.new_event_loop()
        self.ctx = None
        self.start()

    def start(self) -> None:
        """(Re)start the application: a new provider, price client and indexer
        on the same database — what a container restart is."""
        settings = make_settings(self.tmp, alchemy_url="https://eth-mainnet.g.alchemy.com/v2/LAB",
                                 balance_refresh=self.params["balance_refresh"],
                                 sync_interval=self.params["sync_interval"],
                                 backfill_days=self.params["backfill_days"],
                                 reorg_depth=self.params["reorg_depth"],
                                 price_refresh=self.params["price_refresh"],
                                 full_scan_interval=self.params["full_scan_interval"],
                                 max_token_lookups=200)
        config = make_config(wallets=[{"name": f"W{i + 1}", "address": w}
                                      for i, w in enumerate(self.wallets)])
        client = httpx.AsyncClient(transport=self.server.transport())
        provider = AlchemyProvider(settings.alchemy_url, client=client, max_concurrency=2,
                                   compute_units_per_second=self.params.get("cu_per_second", 0))
        prices = AlchemyPrices("LAB", base_url="https://api.g.alchemy.com/prices/v1", client=client)
        if self.ctx is not None:
            self.ctx.db.close()
        self.ctx = build_context(settings, config, provider=provider, price_source=prices)
        self.ctx.db.migrate()
        self.ctx.db.apply_watch_config(config)
        self.ctx.db.relink_accounts()

    next_wait: float | None = None

    def cycle(self, *, blocks: int | None = None) -> tuple[object, list[dict]]:
        """One scheduler tick: the wait the indexer asked for passes (the
        interval, or a back-off after failures), the chain moves on as far as
        that time allows, a cycle runs."""
        wait = self.params["sync_interval"] if self.next_wait is None else self.next_wait
        self.clock.now += wait
        self.server.head += int(wait // BLOCK_SECONDS) if blocks is None else blocks
        result = self.run(self.ctx.indexer.run_once())
        self.next_wait = self.ctx.indexer.next_delay(result)
        return result, self.server.take()

    def run(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def close(self) -> None:
        self.ctx.db.close()
        self.loop.close()
        self.undo()


# ---------------------------------------------- the FakeProvider counter


class CountingProvider:
    """Wraps any ChainDataProvider (the test suite's FakeProvider included)
    and records each call: method, wallet label, outcome. Method level — one
    call here can be several HTTP requests to Alchemy (pages, retries, a
    metadata lookup per token), which FakeAlchemy counts instead."""

    def __init__(self, inner, labels: dict[str, str]) -> None:
        self.inner = inner
        self.labels = labels
        self.calls: list[dict] = []
        self.chain_id = inner.chain_id
        self.name = f"counting-{inner.name}"

    def _wrap(name):
        async def call(self, *args, **kwargs):
            wallet = self.labels.get(args[0]) if args and isinstance(args[0], str) else None
            reason = name
            if name == "get_transfers":
                reason = "transfers-out" if kwargs.get("outgoing") else "transfers-in"
            try:
                result = await getattr(self.inner, name)(*args, **kwargs)
            except Exception:
                self.calls.append({"method": name, "reason": reason, "wallet": wallet, "ok": False})
                raise
            self.calls.append({"method": name, "reason": reason, "wallet": wallet, "ok": True})
            return result
        return call

    get_head_block = _wrap("get_head_block")
    get_native_balance = _wrap("get_native_balance")
    get_token_balances = _wrap("get_token_balances")
    get_transfers = _wrap("get_transfers")
    get_transaction_cost = _wrap("get_transaction_cost")
    del _wrap

    async def close(self) -> None:
        await self.inner.close()


# ------------------------------------------------------------- summaries


def by_method(records: list[dict]) -> Counter:
    return Counter(r["method"] for r in records)


def by_reason(records: list[dict]) -> Counter:
    return Counter(r["reason"] for r in records)


def cu(records: list[dict]) -> int:
    return sum(cu_of(r) for r in records)


def per_day(interval: int, *, per_cycle: float) -> float:
    return per_cycle * 86_400 / interval


def steady_state(lab: Lab, cycles: int, **kwargs) -> list[list[dict]]:
    return [lab.cycle(**kwargs)[1] for _ in range(cycles)]


def warm(lab: Lab) -> None:
    """The first cycle backfills and sweeps everything; the next start the
    steady state. Records of both are discarded."""
    lab.cycle()
    lab.cycle()
    lab.server.take()


def main() -> None:  # pragma: no cover — prints the report's tables
    from tests.quota_scenarios import report
    print(report())


if __name__ == "__main__":  # pragma: no cover
    main()
