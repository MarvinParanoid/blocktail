"""Chain-neutral domain types.

Nothing in here knows what Ethereum is: an ``Asset`` has a contract address and
decimals, an ``Activity`` has a chain id and raw integer amount. Chain-specific
behaviour (address validation, explorer URLs, display formatting) lives behind
the ``Chain`` protocol in :mod:`app.chains`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

NATIVE = ""  # sentinel contract address for a chain's native asset


class TransferKind(StrEnum):
    """How a transfer was carried on-chain."""

    NATIVE = "native"      # a plain value-bearing transaction
    TOKEN = "token"        # a token-contract transfer event
    INTERNAL = "internal"  # value moved by contract execution


class Direction(StrEnum):
    ALL = "all"
    IN = "in"
    OUT = "out"


class Scope(StrEnum):
    """Which accounts the feed is looking through."""

    ALL = "all"          # every monitored account, owned or merely watched
    OWNED = "mine"       # only the accounts that are mine
    WATCHED = "watched"  # only the ones I am watching


@dataclass(frozen=True, slots=True)
class AssetRef:
    """Identifies an asset on a chain. ``contract_address == NATIVE`` is the
    chain's own currency (ETH, and on another chain whatever that chain uses)."""

    chain_id: str
    contract_address: str
    symbol: str
    decimals: int

    @property
    def is_native(self) -> bool:
        return self.contract_address == NATIVE


@dataclass(frozen=True, slots=True)
class Transfer:
    """A single normalized value movement, as returned by a provider.

    ``event_key`` is the stable identity used for deduplication. One transaction
    can produce several transfers, so it must distinguish them within a tx.
    """

    chain_id: str
    event_key: str
    tx_hash: str
    block_number: int
    block_timestamp: int  # unix seconds
    kind: TransferKind
    from_address: str
    to_address: str
    asset: AssetRef
    amount_raw: int


@dataclass(frozen=True, slots=True)
class Balance:
    asset: AssetRef
    amount_raw: int


@dataclass(frozen=True, slots=True)
class TransactionCost:
    """What a transaction cost to execute, as the chain reports it.

    Not indexed: it is read on demand when someone opens a transaction, because
    it is the only thing about a transfer that no amount of transfer data can
    tell you, and polling for it on every row we store would be a call per row
    for a number almost nobody looks at.
    """

    fee_raw: int          # in the native asset's smallest unit
    gas_used: int
    gas_limit: int | None  # None when the transaction itself was not read
    succeeded: bool


@dataclass(frozen=True, slots=True)
class Account:
    """A monitored account (a "wallet" in the UI).

    ``is_owned`` separates "this is mine" from "I am watching this". Both are
    indexed and browsable; only owned accounts are summed into a portfolio.
    """

    id: int
    chain_id: str
    address: str  # canonical form for the chain (lowercase hex on Ethereum)
    name: str
    active: bool = True
    is_owned: bool = True
    source: str = "config"
    # Highest block this account has been scanned to. None means never scanned,
    # so the indexer reaches back through the backfill window for it.
    synced_to_block: int | None = None
    # Lowest block scanned. Widening the backfill window has to reach past this
    # or asking for more history would change nothing.
    indexed_from_block: int | None = None


@dataclass(slots=True)
class SyncStatus:
    chain_id: str
    last_synced_block: int | None = None
    head_block: int | None = None
    last_success_at: int | None = None
    last_attempt_at: int | None = None
    last_error: str | None = None
    backfill_done: bool = False
    requested_from_block: int | None = None
    activity_count: int = 0
