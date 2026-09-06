"""Test harness.

There is no live provider here, so a ``FakeProvider`` replays transfers in the
exact shape ``AlchemyProvider`` produces. Everything below it -- normalization,
persistence, dedup, the HTTP layer -- is the real code path.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings, WatchConfig, parse_watch_config
from app.models import NATIVE, AssetRef, Balance, Transfer, TransferKind

ETH = AssetRef("ethereum", NATIVE, "ETH", 18)
USDC = AssetRef("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC", 6)
SPAM = AssetRef("ethereum", "0xdeadbeef00000000000000000000000000000001", "FREE-AIRDROP", 18)


def address(seed: str) -> str:
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()[:40]


def tx_hash(seed: str) -> str:
    return "0x" + hashlib.sha256(f"tx:{seed}".encode()).hexdigest()


MAIN = address("main")
COLD = address("cold")
PAYMENTS = address("payments")
BINANCE = address("binance")
STRANGER = address("stranger")

BASE_TIME = 1_760_000_000


def transfer(
    seed: str,
    *,
    sender: str,
    recipient: str,
    amount: int,
    asset: AssetRef = ETH,
    kind: TransferKind = TransferKind.NATIVE,
    block: int = 21_000_000,
    log_index: int | None = None,
    trace: str | None = None,
    timestamp: int | None = None,
) -> Transfer:
    """Build a transfer whose ``event_key`` follows Alchemy's uniqueId scheme."""
    digest = tx_hash(seed)
    if kind is TransferKind.TOKEN:
        event_key = f"{digest}:log:{log_index if log_index is not None else 0}"
    elif kind is TransferKind.INTERNAL:
        event_key = f"{digest}:internal:{trace or '0'}"
    else:
        event_key = f"{digest}:external"

    return Transfer(
        chain_id="ethereum",
        event_key=event_key,
        tx_hash=digest,
        block_number=block,
        block_timestamp=timestamp if timestamp is not None else BASE_TIME + (block - 21_000_000) * 12,
        kind=kind,
        from_address=sender.lower(),
        to_address=recipient.lower(),
        asset=asset,
        amount_raw=amount,
    )


class ProviderDown(RuntimeError):
    pass


@dataclass
class FakeProvider:
    """In-memory stand-in for a ChainDataProvider."""

    transfers: list[Transfer] = field(default_factory=list)
    head_block: int = 21_000_100
    balance_calls: list[str] = field(default_factory=list)
    costs: dict[str, object] = field(default_factory=dict)
    native_balances: dict[str, int] = field(default_factory=dict)
    token_balances: dict[str, list[Balance]] = field(default_factory=dict)
    fail_with: Exception | None = None
    fail_transfers_with: Exception | None = None
    fail_balances_with: Exception | None = None
    calls: list[tuple[str, str, bool, int, int]] = field(default_factory=list)

    chain_id: str = "ethereum"
    name: str = "fake"

    async def get_head_block(self) -> int:
        if self.fail_with:
            raise self.fail_with
        return self.head_block

    async def get_native_balance(self, address: str) -> int:
        self.balance_calls.append(address.lower())
        if self.fail_balances_with:
            raise self.fail_balances_with
        return self.native_balances.get(address.lower(), 0)

    async def get_token_balances(self, address: str) -> list[Balance]:
        if self.fail_balances_with:
            raise self.fail_balances_with
        return list(self.token_balances.get(address.lower(), []))

    async def get_transaction_cost(self, tx_hash: str):
        return self.costs.get(tx_hash.lower())

    async def get_transfers(
        self, address: str, *, outgoing: bool, from_block: int, to_block: int
    ) -> list[Transfer]:
        if self.fail_with or self.fail_transfers_with:
            raise self.fail_transfers_with or self.fail_with
        self.calls.append(("get_transfers", address, outgoing, from_block, to_block))
        needle = address.lower()
        return [
            item
            for item in self.transfers
            if from_block <= item.block_number <= to_block
            and (item.from_address == needle if outgoing else item.to_address == needle)
        ]

    async def close(self) -> None:
        return None


@dataclass
class FakePrices:
    """Stand-in PriceSource. Assets absent from ``quotes`` stay unpriced."""

    quotes: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    fail_with: Exception | None = None
    calls: int = 0
    currency: str = "USD"

    async def get_prices(self, assets):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        return {
            (a.chain_id, a.contract_address): self.quotes[(a.chain_id, a.contract_address)]
            for a in assets
            if (a.chain_id, a.contract_address) in self.quotes
        }

    async def close(self) -> None:
        return None


@pytest.fixture
def prices() -> FakePrices:
    return FakePrices(
        quotes={
            ("ethereum", NATIVE): Decimal("3120.55"),
            ("ethereum", USDC.contract_address): Decimal("1.0"),
        }
    )


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        config_path=tmp_path / "wallets.yml",
        database_path=tmp_path / "blocktail.db",
        alchemy_url="http://provider.invalid/v2/key",
        alchemy_api_key="",
        sync_interval=60,
        backfill_days=90,
        reorg_depth=8,
        max_token_balances=10,
        max_token_lookups=200,
        provider_concurrency=2,
        stale_after=180,
        price_refresh=300,
        balance_refresh=0,  # sweep every cycle; the policy has its own tests
        prices_enabled=True,
        value_max_share=0.9,
        dust_below_usd=Decimal("1"),
        dust_transfer_usd=Decimal("0.01"),
        prices_base_url="http://prices.invalid/v1",
        host="127.0.0.1",
        port=8000,
        log_level="WARNING",
        auth_user="",
        auth_password="",
    )
    values.update(overrides)
    return Settings(**values)


def make_config(**overrides) -> WatchConfig:
    raw = {
        "wallets": [
            {"name": "Main", "chain": "ethereum", "address": MAIN},
            {"name": "Cold", "address": COLD},
            {"name": "Payments", "address": PAYMENTS},
        ],
        "labels": {BINANCE: "Binance"},
    }
    raw.update(overrides)
    return parse_watch_config(raw, source="test")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def watch_config() -> WatchConfig:
    return make_config()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(
        native_balances={
            MAIN: 4_821_000_000_000_000_000,
            COLD: 31_420_000_000_000_000_000,
            PAYMENTS: 182_000_000_000_000_000,
        },
        token_balances={
            COLD: [Balance(USDC, 500_000_000), Balance(SPAM, 10**21)],
            MAIN: [Balance(USDC, 1_250_500_000)],
        },
    )


@pytest.fixture
def sample_transfers() -> list[Transfer]:
    """One of each supported movement, including a wallet-to-wallet transfer."""
    return [
        transfer("in-eth", sender=STRANGER, recipient=MAIN, amount=2_400_000_000_000_000_000, block=21_000_010),
        transfer("out-eth", sender=MAIN, recipient=STRANGER, amount=120_000_000_000_000_000, block=21_000_020),
        transfer(
            "out-usdc",
            sender=COLD,
            recipient=BINANCE,
            amount=500_000_000,
            asset=USDC,
            kind=TransferKind.TOKEN,
            log_index=3,
            block=21_000_030,
        ),
        transfer(
            "internal-in",
            sender=STRANGER,
            recipient=PAYMENTS,
            amount=50_000_000_000_000_000,
            kind=TransferKind.INTERNAL,
            trace="0_1",
            block=21_000_040,
        ),
        transfer(
            "wallet-to-wallet",
            sender=MAIN,
            recipient=COLD,
            amount=5_000_000_000_000_000_000,
            block=21_000_050,
        ),
    ]


@pytest.fixture
def app_ctx(settings, watch_config, provider):
    """A migrated database with the config applied, ready for the indexer."""
    from app.main import build_context

    ctx = build_context(settings, watch_config, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(watch_config)
    ctx.db.relink_accounts()
    yield ctx
    ctx.db.close()


def run(coro):
    return asyncio.run(coro)
