"""A local blocktail for the browser tests: the real app, the real indexer and a
temporary SQLite database, fed by an in-memory provider with fixed data.

Nothing here talks to a network. The provider is the test suite's
FakeProvider; prices are FakePrices; the key and the URLs are placeholders.

The data is deterministic: the same wallets, transfers and balances every
run. Timestamps are relative to the moment the stand starts, so "Today" and
"Yesterday" always have rows under them.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import create_app
from app.models import NATIVE, AssetRef, Balance, TransactionCost, TransferKind
from tests.conftest import (
    FakePrices,
    FakeProvider,
    address,
    make_config,
    make_settings,
    tx_hash,
)
from tests.conftest import transfer as make_transfer

C = "ethereum"
E18 = 10**18
HEAD = 23_800_000
DAY_BLOCKS = 7200

ETH = AssetRef(C, NATIVE, "ETH", 18)
USDC = AssetRef(C, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC", 6)
USDT = AssetRef(C, "0xdac17f958d2ee523a2206206994597c13d831ec7", "USDT", 6)
WETH = AssetRef(C, "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "WETH", 18)
LINK = AssetRef(C, "0x514910771af9ca656af840dff83e8264ecf986ca", "LINK", 18)
# A scam token with an absurd supply and a nominal quote: excluded from totals,
# and the source of the known narrow-screen overflow (audit U-03).
NMT = AssetRef(C, address("nmt-scam")[:42], "NMT", 18)
# A forged ticker (Lisu letters that read as USDT): hidden as an impostor.
FORGED = AssetRef(C, address("forged-usdt")[:42], "ꓴꓢꓓ⊤", 6)

MAIN = address("ui-main")
COLD = address("ui-cold")
PAY = address("ui-payments")
TREASURY = address("ui-treasury")
TRADE = address("ui-trading")
WHALE = address("ui-whale")
STRANGERS = [address(f"ui-stranger-{i}") for i in range(12)]
BINANCE = address("ui-binance")
# Not in the config: added from the page by a test, then removed.
REMOVABLE = address("ui-removable")

WALLETS = [
    {"name": "Main", "address": MAIN},
    {"name": "Cold", "address": COLD},
    {"name": "Payments", "address": PAY},
    {"name": "Treasury multisig — operations reserve (legacy Safe, 2021)", "address": TREASURY},
    {"name": "Trading", "address": TRADE},
    {"name": "Whale #1", "address": WHALE, "owned": False},
]

PRICES = {
    (C, NATIVE): Decimal("3120.55"),
    (C, USDC.contract_address): Decimal(1),
    (C, USDT.contract_address): Decimal(1),
    (C, WETH.contract_address): Decimal("3119.80"),
    (C, LINK.contract_address): Decimal("14.21"),
    (C, NMT.contract_address): Decimal("0.42"),
    (C, FORGED.contract_address): Decimal(1),
}

# The swap the inspector tests open: one transaction, four transfers.
SWAP_SEED = "ui-swap"


class GatedProvider(FakeProvider):
    """Reading history older than the backfill window waits for a gate the
    test opens, so the page can be seen while a request is running."""

    def __init__(self, *args, deep_below: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.deep_below = deep_below
        self.gate = threading.Event()
        self.gate.set()

    async def get_transfers(self, address, *, outgoing, from_block, to_block):
        if from_block < self.deep_below:
            while not self.gate.is_set():
                await asyncio.sleep(0.05)
        return await super().get_transfers(address, outgoing=outgoing, from_block=from_block,
                                           to_block=to_block)


def _transfers(now: int) -> list:
    out = []

    def add(seed, sender, recipient, amount, *, blocks_ago, asset=ETH,
            kind=TransferKind.NATIVE, log_index=None, trace=None):
        block = HEAD - blocks_ago
        out.append(make_transfer(seed, sender=sender, recipient=recipient, amount=amount,
                                 asset=asset, kind=kind, block=block, log_index=log_index,
                                 trace=trace, timestamp=now - blocks_ago * 12))

    # A month of ordinary traffic, spread over the wallets.
    owners = [MAIN, COLD, PAY, TREASURY, TRADE, WHALE]
    for i in range(90):
        owner = owners[i % len(owners)]
        other = STRANGERS[i % len(STRANGERS)]
        incoming = i % 3 != 0
        sender, recipient = (other, owner) if incoming else (owner, other)
        if i % 4 == 0:
            add(f"ui-usdc-{i}", sender, recipient, (i + 1) * 1_250_000, blocks_ago=37 + i * 2300,
                asset=USDC, kind=TransferKind.TOKEN, log_index=i % 7)
        else:
            add(f"ui-eth-{i}", sender, recipient, (i + 1) * 37 * 10**15, blocks_ago=37 + i * 2300)

    # Between two of my wallets, and from mine to a watched one.
    add("ui-main-to-cold", MAIN, COLD, 5 * E18, blocks_ago=120)
    add("ui-main-to-whale", MAIN, WHALE, 2 * E18 // 10, blocks_ago=3 * DAY_BLOCKS)
    # Labelled counterparty.
    add("ui-to-binance", COLD, BINANCE, 1_000 * 10**6, blocks_ago=900, asset=USDC,
        kind=TransferKind.TOKEN, log_index=2)
    # A swap: four transfers in one transaction.
    add(SWAP_SEED, MAIN, STRANGERS[0], 5 * E18, blocks_ago=200)
    add(SWAP_SEED, STRANGERS[0], MAIN, 15_603 * 10**6, blocks_ago=200, asset=USDC,
        kind=TransferKind.TOKEN, log_index=10)
    add(SWAP_SEED, STRANGERS[1], MAIN, 2 * E18, blocks_ago=200, asset=WETH,
        kind=TransferKind.TOKEN, log_index=11)
    add(SWAP_SEED, STRANGERS[2], MAIN, 10**16, blocks_ago=200, kind=TransferKind.INTERNAL,
        trace="0_1")
    # Extremes: the scam token's absurd amount, a forged ticker, dust.
    add("ui-nmt", STRANGERS[3], TRADE, 10**30 * E18, blocks_ago=1500, asset=NMT,
        kind=TransferKind.TOKEN, log_index=4)
    add("ui-forged", STRANGERS[4], MAIN, 10_000 * 10**6, blocks_ago=1800, asset=FORGED,
        kind=TransferKind.TOKEN, log_index=5)
    add("ui-dust", STRANGERS[5], MAIN, 1, blocks_ago=2500)
    # History older than the backfill window: reached by "Load more".
    add("ui-ancient", STRANGERS[6], MAIN, 3 * E18, blocks_ago=40 * DAY_BLOCKS)
    # The wallet a test adds and removes has a history of its own.
    add("ui-removable-in", STRANGERS[7], REMOVABLE, 4 * E18, blocks_ago=60)
    return out


def _balances() -> dict:
    return {
        MAIN: [Balance(USDC, 12_500 * 10**6), Balance(LINK, 412 * E18),
               Balance(FORGED, 10_000 * 10**6)],
        COLD: [Balance(USDC, 500 * 10**6), Balance(WETH, 3 * E18)],
        PAY: [Balance(USDT, 12_000 * 10**6)],
        TREASURY: [Balance(USDC, 1_000_000 * 10**6)],
        TRADE: [Balance(NMT, 10**30 * E18), Balance(LINK, 5 * E18)],
        WHALE: [Balance(USDC, 50_000_000 * 10**6)],
    }


class Stand:
    """Start it, use ``url``, stop it. One per test session."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.now = int(time.time())
        self.provider = GatedProvider(
            head_block=HEAD,
            deep_below=HEAD - 30 * DAY_BLOCKS,
            transfers=_transfers(self.now),
            native_balances={MAIN: 4_821 * 10**15, COLD: 31_420 * 10**15, PAY: 182 * 10**15,
                             TREASURY: 1_200 * E18, TRADE: 7 * 10**17, WHALE: 9_000 * E18},
            token_balances=_balances(),
            costs={},
        )
        self.provider.costs = {tx_hash(SWAP_SEED): TransactionCost(
            fee_raw=252_000 * 10**9, gas_used=126_000, gas_limit=180_000, succeeded=True,
        )}
        settings = make_settings(
            workdir,
            sync_interval=3600,      # cycles run on start and on a wake, never on a timer
            backfill_days=30,
            reorg_depth=8,
            stale_after=3600,
            price_refresh=3600,
            balance_refresh=3600,
        )
        config = make_config(wallets=WALLETS, labels={BINANCE: "Binance hot"})
        self.app = create_app(settings=settings, config=config, provider=self.provider,
                              price_source=FakePrices(quotes=dict(PRICES)), run_indexer=True)
        # Stand-only: lets a test ask for a cycle the way a request for older
        # history does. Not part of the product.
        indexer = self.app.state.ctx.indexer

        @self.app.post("/__stand__/sync")
        async def _sync():
            indexer.wake()
            return {"woken": True}

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = workdir / "server.log"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def db(self):
        return self.app.state.ctx.db

    def start(self) -> Stand:
        # The app's and the server's log lines, kept for the failure artifacts.
        self._log_handler = logging.FileHandler(self.log_path)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.INFO)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._server.started:
                status = self.db.get_sync_status(C)
                if status.backfill_done and status.activity_count:
                    return self
            time.sleep(0.05)
        raise RuntimeError("the stand did not finish its first sync in 30 s")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
        if getattr(self, "_log_handler", None) is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler.close()

    def account_id(self, name: str) -> int:
        return next(a.id for a in self.db.list_accounts() if a.name == name)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
