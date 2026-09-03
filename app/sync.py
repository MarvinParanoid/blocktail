"""The indexer: pull new activity from the chain provider, normalize, persist.

Design rules that matter here:

* **Idempotent.** Every transfer carries a stable ``event_key``; storing is an
  upsert on ``(chain_id, event_key)``. Restarting, or re-scanning a block range,
  never duplicates an event.
* **Overlapping re-scan.** Each cycle re-reads the last ``reorg_depth`` blocks.
  Anything the re-scan no longer reports inside that window is deleted, so a
  reorged-out transfer does not linger forever.
* **Per-account watermark.** Each account records how far it has been scanned.
  A new one reaches back through the backfill window; one that was archived and
  re-added resumes from where it stopped, rather than replaying its history or
  silently skipping the gap.
* **No partial progress on failure.** A cycle either completes for every wallet
  and advances the watermark, or it records the error and leaves it alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.chains import Chain, ChainDataProvider
from app.config import Settings, WatchConfig
from app.db import Database
from app.models import NATIVE, Account, AssetRef, Balance, Transfer
from app.prices import PriceSource

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CycleResult:
    chain_id: str
    head_block: int
    from_block: int
    fetched: int = 0
    stored: int = 0
    pruned: int = 0
    balances_updated: int = 0
    prices_updated: int = 0
    duration: float = 0.0
    error: str | None = None
    backfill: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


class Indexer:
    def __init__(
        self,
        *,
        db: Database,
        provider: ChainDataProvider,
        chain: Chain,
        config: WatchConfig,
        settings: Settings,
        price_source: PriceSource | None = None,
    ) -> None:
        self.db = db
        self.provider = provider
        self.price_source = price_source
        self._priced_at = 0.0
        self.chain = chain
        self.config = config
        self.settings = settings
        self.chain_id = chain.chain_id
        self._stop = asyncio.Event()

    # --------------------------------------------------------------- loop

    async def run_forever(self) -> None:
        log.info(
            "indexer started for %s (interval %ss, reorg depth %s)",
            self.chain_id,
            self.settings.sync_interval,
            self.settings.reorg_depth,
        )
        while not self._stop.is_set():
            try:
                result = await self.run_once()
                if result.ok:
                    log.info(
                        "sync %s: blocks %s-%s, %s fetched, %s new, %s pruned in %.1fs",
                        self.chain_id,
                        result.from_block,
                        result.head_block,
                        result.fetched,
                        result.stored,
                        result.pruned,
                        result.duration,
                    )
                else:
                    log.error("sync %s failed: %s", self.chain_id, result.error)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug here must not kill the loop
                log.exception("unexpected error in sync cycle")
                await asyncio.to_thread(
                    self.db.record_failure, self.chain_id, "internal error during sync"
                )

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.sync_interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------- cycle

    async def run_once(self) -> CycleResult:
        started = time.monotonic()
        await asyncio.to_thread(self.db.record_attempt, self.chain_id)

        accounts = [
            account
            for account in await asyncio.to_thread(self.db.list_accounts)
            if account.chain_id == self.chain_id
        ]
        status = await asyncio.to_thread(self.db.get_sync_status, self.chain_id)

        try:
            head = await self.provider.get_head_block()
        except Exception as exc:
            await asyncio.to_thread(self.db.record_failure, self.chain_id, str(exc))
            return CycleResult(self.chain_id, 0, 0, error=str(exc))

        backfill_from = max(0, head - self.settings.backfill_blocks)
        if status.last_synced_block is None:
            from_block = backfill_from
        else:
            from_block = max(0, status.last_synced_block - self.settings.reorg_depth + 1)

        # An account behind the shared watermark reaches further back, so the
        # window actually scanned is the earliest of them. The prune window is
        # derived from `from_block`, never from this.
        starts = {account.id: self._account_start(account, from_block, backfill_from)
                  for account in accounts}
        earliest = min(starts.values(), default=from_block)
        result = CycleResult(
            self.chain_id,
            head,
            earliest,
            backfill=status.last_synced_block is None or earliest < from_block,
        )
        if not accounts:
            log.warning("no wallets configured for %s; nothing to index", self.chain_id)
            await asyncio.to_thread(
                self.db.record_success, self.chain_id, last_synced_block=head, head_block=head
            )
            result.duration = time.monotonic() - started
            return result

        try:
            transfers = await self._fetch_transfers(accounts, starts, head)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            await asyncio.to_thread(self.db.record_failure, self.chain_id, message)
            result.error = message
            result.duration = time.monotonic() - started
            return result

        result.fetched = len(transfers)
        account_index = {(a.chain_id, a.address): a.id for a in accounts}
        result.stored = await asyncio.to_thread(self.db.store_transfers, transfers, account_index)
        result.pruned = await asyncio.to_thread(
            self._prune, transfers, from_block, head
        )

        try:
            result.balances_updated = await self._refresh_balances(accounts)
        except Exception as exc:
            # Balances are secondary: a failure there should not discard the
            # activity we just indexed, but it must still be visible.
            message = f"balance refresh failed: {type(exc).__name__}: {exc}"
            log.warning(message)
            await asyncio.to_thread(self.db.record_failure, self.chain_id, message)
            result.error = message
            result.duration = time.monotonic() - started
            return result

        result.prices_updated = await self._refresh_prices()

        behind = [account for account in accounts if starts[account.id] < from_block]
        if behind:
            log.info(
                "caught up %d account(s) from block %s",
                len(behind),
                min(starts[account.id] for account in behind),
            )
        await asyncio.to_thread(self.db.mark_synced, starts, head)

        await asyncio.to_thread(
            self.db.record_success, self.chain_id, last_synced_block=head, head_block=head
        )
        result.duration = time.monotonic() - started
        return result

    # --------------------------------------------------------------- prices

    async def _refresh_prices(self, *, force: bool = False) -> int:
        """Re-price held assets on their own slower cadence.

        A price failure never fails the cycle: on-chain activity is the product,
        valuation is a convenience. What it must not do is leave a stale number
        looking current, which is why the age is stored and shown.
        """
        if self.price_source is None:
            return 0
        now = time.monotonic()
        if not force and now - self._priced_at < self.settings.price_refresh:
            return 0

        rows = await asyncio.to_thread(self.db.held_assets)
        if not rows:
            self._priced_at = now
            return 0

        assets = [
            AssetRef(row["chain_id"], row["contract_address"], row["symbol"], row["decimals"])
            for row in rows
        ]
        by_key = {(row["chain_id"], row["contract_address"]): row["id"] for row in rows}

        try:
            quotes = await self.price_source.get_prices(assets)
        except Exception as exc:
            log.warning("price refresh failed: %s: %s", type(exc).__name__, exc)
            return 0

        priced = {by_key[key]: str(value) for key, value in quotes.items() if key in by_key}
        if priced:
            await asyncio.to_thread(
                self.db.replace_prices, priced, self.price_source.currency
            )
        self._priced_at = now
        if len(priced) < len(assets):
            log.info("priced %d of %d held assets", len(priced), len(assets))
        return len(priced)

    # ------------------------------------------------------------- fetching

    def _account_start(self, account: Account, from_block: int, backfill_from: int) -> int:
        """Where this account's scan begins.

        Never scanned: the backfill window. Already scanned: its own watermark,
        overlapped by the reorg depth — so an account archived for a while closes
        its own gap instead of restarting at the head.

        And if the configured window now reaches further back than this account
        has ever been read, it reaches back: otherwise raising BACKFILL_DAYS
        would quietly do nothing.
        """
        if account.synced_to_block is None:
            return backfill_from
        if account.indexed_from_block is None or backfill_from < account.indexed_from_block:
            return backfill_from
        return max(0, min(from_block, account.synced_to_block - self.settings.reorg_depth + 1))

    async def _fetch_transfers(
        self, accounts: list[Account], starts: dict[int, int], to_block: int
    ) -> list[Transfer]:
        tasks = [
            self.provider.get_transfers(
                account.address,
                outgoing=outgoing,
                from_block=starts[account.id],
                to_block=to_block,
            )
            for account in accounts
            for outgoing in (True, False)
        ]
        batches = await asyncio.gather(*tasks)

        # The same transfer arrives twice when both sides are monitored; the
        # event key collapses them here as well as in the database.
        merged: dict[str, Transfer] = {}
        for batch in batches:
            for transfer in batch:
                if self._is_ignored(transfer):
                    continue
                merged[transfer.event_key] = transfer
        return list(merged.values())

    def _is_ignored(self, transfer: Transfer) -> bool:
        asset = transfer.asset
        return (asset.chain_id, asset.contract_address) in self.config.ignore_assets

    def _prune(self, transfers: list[Transfer], from_block: int, head: int) -> int:
        """Delete events inside the re-scanned window that the chain no longer
        reports. Only the window is authoritative, so only it is pruned."""
        prune_from = max(from_block, head - self.settings.reorg_depth)
        if prune_from > head:
            return 0
        observed = {
            transfer.event_key
            for transfer in transfers
            if prune_from <= transfer.block_number <= head
        }
        known = self.db.known_event_keys(self.chain_id, prune_from, head)
        stale = known - observed
        if not stale:
            return 0
        log.info("pruning %d event(s) no longer on chain in blocks %s-%s", len(stale), prune_from, head)
        return self.db.drop_activities(self.chain_id, stale)

    async def _refresh_balances(self, accounts: list[Account]) -> int:
        native_asset = AssetRef(
            chain_id=self.chain_id,
            contract_address=NATIVE,
            symbol=self.chain.native_symbol,
            decimals=self.chain.native_decimals,
        )

        async def for_account(account: Account) -> tuple[int, list[Balance]]:
            native, tokens = await asyncio.gather(
                self.provider.get_native_balance(account.address),
                self.provider.get_token_balances(account.address),
            )
            balances = [Balance(asset=native_asset, amount_raw=native)]
            balances.extend(
                token
                for token in tokens
                if token.amount_raw > 0
                and (self.chain_id, token.asset.contract_address) not in self.config.ignore_assets
            )
            return account.id, balances

        for account_id, balances in await asyncio.gather(*(for_account(a) for a in accounts)):
            await asyncio.to_thread(self.db.replace_balances, account_id, balances)
        return len(accounts)
