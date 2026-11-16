"""The indexer: pull new activity from the chain provider, normalize, persist.

Design rules that matter here:

* **Idempotent.** Every transfer carries a stable ``event_key``; storing is an
  upsert on ``(chain_id, event_key)``. Restarting, or re-scanning a block range,
  never duplicates an event.
* **One cycle at a time.** A cycle is single-flight: whoever asks while one is
  running joins it rather than starting another. Only the scheduler loop starts
  cycles; the web layer asks for one with ``wake()``.
* **Overlapping re-scan.** Each cycle re-reads the last ``reorg_depth`` blocks
  — more after a downtime, everything since the last success. Anything that
  re-scan no longer reports is deleted, so a reorged-out transfer does not
  linger forever.
* **Per-account watermark.** Each account records how far it has been scanned.
  A new one reaches back through the backfill window; one that was archived and
  re-added resumes from where it stopped, rather than replaying its history or
  silently skipping the gap.
* **No partial progress on failure — of indexing.** Transfers are fetched for
  every wallet or the cycle records the error and leaves the watermark alone.
  Balances come after and fail on their own: indexed history is kept and its
  watermark advanced, and the wallets whose balances failed are read again
  next cycle.
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
    error: str | None = None          # indexing failed; nothing advanced
    balance_error: str | None = None  # indexing succeeded; some balances did not
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
        # `None`, not 0.0. These are compared against `time.monotonic()`, whose
        # origin is arbitrary — on Linux it counts from boot. Zero as a stand-in
        # for "never" means "boot time", so on a machine that has been up for
        # hours the first refresh happens and on one freshly booted it is
        # silently skipped for a whole interval. It worked everywhere it was
        # tried and failed on CI, which is what a fresh machine looks like.
        self._priced_at: float | None = None
        self._balances_at: float | None = None
        self.chain = chain
        self.config = config
        self.settings = settings
        self.chain_id = chain.chain_id
        self._stop = asyncio.Event()
        # Set by anyone with work for the next cycle (a history request); the
        # scheduler wakes early instead of waiting out the interval. An Event,
        # not a queue: any number of wakes before the next cycle is one cycle.
        self._wake = asyncio.Event()
        self._inflight: asyncio.Task[CycleResult] | None = None
        # Wallets whose balances failed; read again next cycle whether or not
        # they moved. In memory only: a restart sweeps every wallet anyway.
        self._balances_pending: set[int] = set()

    # --------------------------------------------------------------- loop

    async def run_forever(self) -> None:
        log.info(
            "indexer started for %s (interval %ss, reorg depth %s)",
            self.chain_id,
            self.settings.sync_interval,
            self.settings.reorg_depth,
        )
        while not self._stop.is_set():
            # Cleared before the cycle, so a wake that arrives during it is
            # not lost: it makes the next cycle start straight away.
            self._wake.clear()
            try:
                result = await self.run_once()
                self._log(result)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug here must not kill the loop
                log.exception("unexpected error in sync cycle")
                await asyncio.to_thread(
                    self.db.record_failure, self.chain_id, "internal error during sync"
                )
            await self._wait_for_work()

    async def _wait_for_work(self) -> None:
        """Sleep until the interval passes, someone wakes us, or we stop. No
        cycle is running here, so nothing is held while waiting."""
        waiters = [
            asyncio.ensure_future(self._stop.wait()),
            asyncio.ensure_future(self._wake.wait()),
        ]
        try:
            await asyncio.wait(
                waiters, timeout=self.settings.sync_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                waiter.cancel()

    def _log(self, result: CycleResult) -> None:
        if not result.ok:
            log.error("sync %s failed: %s", self.chain_id, result.error)
            return
        log.info(
            "sync %s: blocks %s-%s, %s fetched, %s new, %s pruned in %.1fs",
            self.chain_id, result.from_block, result.head_block, result.fetched,
            result.stored, result.pruned, result.duration,
        )
        if result.balance_error:
            log.warning("sync %s: %s", self.chain_id, result.balance_error)

    def wake(self) -> None:
        """Ask for a cycle soon. Never runs one: the scheduler does."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        if self._inflight is not None and not self._inflight.done():
            self._inflight.cancel()

    async def wait_idle(self) -> None:
        """Return once no cycle is running — after stop(), once the cancelled
        one has actually unwound, so the provider and database can be closed."""
        task = self._inflight
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def busy(self) -> bool:
        return self._inflight is not None and not self._inflight.done()

    # -------------------------------------------------------------- cycle

    async def run_once(self) -> CycleResult:
        """Run a cycle, or join the one already running.

        Single-flight: two callers at once get one cycle and the same result,
        and there is never a queue of cycles behind it. The caller that starts
        a cycle owns it — cancelling that caller (shutdown) cancels the cycle —
        while a caller that joins waits behind a shield, so giving up on the
        result does not abort the work for everyone else. However the cycle
        ends, the next call starts a fresh one.
        """
        task = self._inflight
        if task is not None and not task.done():
            return await asyncio.shield(task)
        task = asyncio.ensure_future(self._run_cycle())
        self._inflight = task
        task.add_done_callback(self._release)
        return await task

    def _release(self, task: asyncio.Task) -> None:
        if self._inflight is task:
            self._inflight = None

    async def _run_cycle(self) -> CycleResult:
        started = time.monotonic()
        await asyncio.to_thread(self.db.record_attempt, self.chain_id)

        accounts = [
            account
            for account in await asyncio.to_thread(self.db.list_accounts)
            if account.chain_id == self.chain_id
        ]
        status = await asyncio.to_thread(self.db.get_sync_status, self.chain_id)
        reaching_back = status.history_status in ("queued", "running", "failed")
        if reaching_back:
            await asyncio.to_thread(self.db.history_started, self.chain_id)

        async def failed(result: CycleResult, message: str) -> CycleResult:
            await asyncio.to_thread(self.db.record_failure, self.chain_id, message)
            if reaching_back:
                await asyncio.to_thread(self.db.history_finished, self.chain_id, message)
            result.error = message
            result.duration = time.monotonic() - started
            return result

        try:
            head = await self.provider.get_head_block()
        except Exception as exc:
            return await failed(CycleResult(self.chain_id, 0, 0), str(exc))

        # The configured window is a floor for a fresh install; a reader who has
        # asked for more history has asked once and for all.
        backfill_from = max(0, head - self.settings.backfill_blocks)
        if status.requested_from_block is not None:
            backfill_from = max(0, min(backfill_from, status.requested_from_block))
        if status.last_synced_block is None:
            from_block = backfill_from
        else:
            from_block = max(0, status.last_synced_block - self.settings.reorg_depth + 1)

        # An account behind the shared watermark reaches further back, so the
        # window actually scanned is the earliest of them.
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
            if reaching_back:
                await asyncio.to_thread(self.db.history_finished, self.chain_id)
            result.duration = time.monotonic() - started
            return result

        try:
            transfers = await self._fetch_transfers(accounts, starts, head)
        except Exception as exc:
            return await failed(result, f"{type(exc).__name__}: {exc}")

        result.fetched = len(transfers)
        result.stored = await asyncio.to_thread(self.db.store_transfers, transfers)
        result.pruned = await asyncio.to_thread(
            self._prune, transfers, self._prune_windows(accounts, starts, head), head
        )

        # Indexing is done and complete: what was read is stored, and the
        # watermark moves now — before balances, which can fail on their own
        # without sending the next cycle back over the same blocks.
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
        if reaching_back:
            await asyncio.to_thread(self.db.history_finished, self.chain_id)

        updated, balance_error = await self._refresh_balances(
            self._needs_balances(accounts, transfers, forced=result.backfill)
        )
        result.balances_updated = updated
        result.balance_error = balance_error
        if updated or balance_error:
            await asyncio.to_thread(self.db.record_balances, self.chain_id, balance_error)

        result.prices_updated = await self._refresh_prices()
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
        if (
            not force
            and self._priced_at is not None
            and now - self._priced_at < self.settings.price_refresh
        ):
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
        # A TaskGroup, not gather: when one wallet's fetch fails, the others
        # are cancelled instead of running on unobserved, spending quota for a
        # cycle that is already lost — and a shutdown cancels all of them.
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(
                        self.provider.get_transfers(
                            account.address,
                            outgoing=outgoing,
                            from_block=starts[account.id],
                            to_block=to_block,
                        )
                    )
                    for account in accounts
                    for outgoing in (True, False)
                ]
        except* Exception as group_error:
            # Report the provider's own error, not "unhandled errors in a TaskGroup".
            raise group_error.exceptions[0] from None
        batches = [task.result() for task in tasks]

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

    def _prune_windows(
        self, accounts: list[Account], starts: dict[int, int], head: int
    ) -> dict[int, int]:
        """For each wallet, the first block of the range it is responsible for
        re-checking this cycle; the range runs to the head.

        A wallet's reorg window starts REORG_DEPTH below where *it* was last
        synced — after a downtime, that covers everything since — clipped to
        where its scan actually started. Windows are per wallet, so a wallet
        that started elsewhere (new, re-added, or reaching back) changes no
        one else's.

        A wallet that was never synced has no window: everything it reads is
        settled history, read for the first time. Neither does the part of a
        scan below a wallet's own window — a deep history request reads
        settled blocks, and a provider that stops serving old history must not
        be able to erase settled records.
        """
        windows: dict[int, int] = {}
        for account in accounts:
            if account.synced_to_block is None:
                continue
            first = max(
                0,
                starts[account.id],
                account.synced_to_block - self.settings.reorg_depth + 1,
            )
            if first <= head:
                windows[account.id] = first
        return windows

    def _prune(self, transfers: list[Transfer], windows: dict[int, int], head: int) -> int:
        """Delete events that a wallet responsible for them re-scanned and the
        chain no longer reports. "Reported" is any scan this cycle: a shared
        event that either side still returns is kept."""
        observed = {transfer.event_key for transfer in transfers}
        stale = self.db.prunable_event_keys(self.chain_id, windows, head) - observed
        if not stale:
            return 0
        log.info(
            "pruning %d event(s) no longer on chain, up to block %s", len(stale), head
        )
        return self.db.drop_activities(self.chain_id, stale)

    def _needs_balances(
        self, accounts: list[Account], transfers: list[Transfer], *, forced: bool
    ) -> list[Account]:
        """Which wallets to re-read balances for this cycle.

        A balance cannot change without a transfer, and we have just read every
        transfer. So the wallets that moved are the wallets to re-read, and
        asking the provider about the other four every thirty seconds buys
        nothing but rate-limit headroom spent. The periodic full sweep is the
        safety net for anything that argument misses, and a wallet whose
        balances failed last time is read again whether it moved or not.
        """
        now = time.monotonic()
        if (
            forced
            or self._balances_at is None
            or now - self._balances_at >= self.settings.balance_refresh
        ):
            self._balances_at = now
            return accounts

        moved = {a for t in transfers for a in (t.from_address, t.to_address)}
        return [
            account for account in accounts
            if account.address in moved or account.id in self._balances_pending
        ]

    async def _refresh_balances(self, accounts: list[Account]) -> tuple[int, str | None]:
        """Re-read balances, one wallet at a time as far as failure goes.

        A wallet whose read fails keeps its last known balances — never zeros
        — and is queued for the next cycle; the others are stored. Returns how
        many were updated and, if any failed, why.
        """
        if not accounts:
            return 0, None

        native_asset = AssetRef(
            chain_id=self.chain_id,
            contract_address=NATIVE,
            symbol=self.chain.native_symbol,
            decimals=self.chain.native_decimals,
        )

        async def for_account(account: Account) -> list[Balance]:
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
            return balances

        outcomes = await asyncio.gather(
            *(for_account(a) for a in accounts), return_exceptions=True
        )
        updated, failures = 0, []
        for account, outcome in zip(accounts, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                self._balances_pending.add(account.id)
                failures.append(f"{account.name}: {type(outcome).__name__}: {outcome}")
                continue
            await asyncio.to_thread(self.db.replace_balances, account.id, outcome)
            self._balances_pending.discard(account.id)
            updated += 1

        if not failures:
            return updated, None
        return updated, (
            f"balances not refreshed for {len(failures)} of {len(accounts)} wallet(s): "
            + "; ".join(failures)
        )
