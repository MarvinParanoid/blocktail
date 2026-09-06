"""SQLite persistence: migrations, writes from the indexer, reads for the UI.

All amounts are stored as decimal strings of the raw integer (wei, token base
units) together with the asset's ``decimals``; nothing is ever put through a
float.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from app.config import WatchConfig
from app.models import (
    Account,
    AssetRef,
    Balance,
    Direction,
    SyncStatus,
    Transfer,
)

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_RE = re.compile(r"^(\d+)_")


class Database:
    """Owns the SQLite file. Connections are per-thread; FastAPI runs sync
    endpoints in a threadpool and the indexer writes from another thread."""

    # One projection for the feed and for a single activity, so a row rendered
    # in the list and the same row opened on its own cannot drift apart.
    _ACTIVITY_SELECT = """
        SELECT a.id, a.chain_id, a.tx_hash, a.block_number, a.block_timestamp, a.kind,
               a.amount_raw, a.from_address, a.to_address,
               a.from_account_id, a.to_account_id,
               a.asset_id, ast.contract_address, ast.symbol, ast.decimals,
               fa.name AS from_account_name, ta.name AS to_account_name,
               fl.label AS from_label, tl.label AS to_label
        FROM activities a
        JOIN assets ast ON ast.id = a.asset_id
        LEFT JOIN accounts fa ON fa.id = a.from_account_id
        LEFT JOIN accounts ta ON ta.id = a.to_account_id
        LEFT JOIN labels fl ON fl.chain_id = a.chain_id AND fl.address = a.from_address
        LEFT JOIN labels tl ON tl.chain_id = a.chain_id AND tl.address = a.to_address
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------ plumbing

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        if conn := getattr(self._local, "conn", None):
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------- migrations

    def migrate(self) -> list[int]:
        """Apply pending migrations. Never destructive: the SQLite file can be
        carried across upgrades."""
        conn = self.connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

        pending = []
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            match = _VERSION_RE.match(sql_file.name)
            if not match:
                continue
            version = int(match.group(1))
            if version not in applied:
                pending.append((version, sql_file))

        for version, sql_file in pending:
            log.info("applying migration %s", sql_file.name)
            # SQLite has transactional DDL, so the schema change and the version
            # row land together or not at all. The BEGIN/COMMIT must live inside
            # the script because executescript() commits before it runs.
            script = (
                "BEGIN;\n"
                f"{sql_file.read_text()}\n"
                "INSERT INTO schema_migrations (version, applied_at)"
                f" VALUES ({version}, {int(time.time())});\n"
                "COMMIT;"
            )
            with self._write_lock:
                try:
                    conn.executescript(script)
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

        return [version for version, _ in pending]

    # ------------------------------------------------- config -> persistence

    def apply_watch_config(self, config: WatchConfig) -> list[Account]:
        """Mirror the config file into the database.

        Wallets removed from the config are deactivated rather than deleted, so
        their history stays intact and reappears if they are re-added.
        """
        conn = self.connect()
        now = int(time.time())
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                # Accounts added through the UI are not in the file and must not
                # be deactivated by its absence.
                conn.execute("UPDATE accounts SET active = 0 WHERE source = 'config'")
                for position, wallet in enumerate(config.wallets):
                    conn.execute(
                        "INSERT INTO accounts"
                        " (chain_id, address, name, position, active, created_at, is_owned, source)"
                        " VALUES (?, ?, ?, ?, 1, ?, ?, 'config')"
                        " ON CONFLICT (chain_id, address) DO UPDATE SET"
                        "   name = excluded.name, position = excluded.position, active = 1,"
                        "   is_owned = excluded.is_owned",
                        (
                            wallet.chain_id,
                            wallet.address,
                            wallet.name,
                            position,
                            now,
                            int(wallet.is_owned),
                        ),
                    )

                conn.execute("DELETE FROM labels")
                conn.executemany(
                    "INSERT INTO labels (chain_id, address, label) VALUES (?, ?, ?)",
                    [(chain, addr, text) for (chain, addr), text in config.labels.items()],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return self.list_accounts()

    def list_accounts(self, *, active_only: bool = True) -> list[Account]:
        sql = (
            "SELECT id, chain_id, address, name, active, is_owned, source,"
            " synced_to_block, indexed_from_block FROM accounts"
        )
        if active_only:
            sql += " WHERE active = 1"
        # Owned accounts lead: the sidebar reads "my wallets", then "watching".
        sql += " ORDER BY is_owned DESC, position, id"
        return [
            Account(
                id=row["id"],
                chain_id=row["chain_id"],
                address=row["address"],
                name=row["name"],
                active=bool(row["active"]),
                is_owned=bool(row["is_owned"]),
                source=row["source"],
                synced_to_block=row["synced_to_block"],
                indexed_from_block=row["indexed_from_block"],
            )
            for row in self.connect().execute(sql)
        ]

    def add_account(
        self, *, chain_id: str, address: str, name: str, is_owned: bool
    ) -> Account:
        """Add an account from the UI. Raises ``ValueError`` if already watched."""
        conn = self.connect()
        existing = conn.execute(
            "SELECT name, active FROM accounts WHERE chain_id = ? AND address = ?",
            (chain_id, address),
        ).fetchone()
        if existing is not None and existing["active"]:
            raise ValueError(f"that address is already watched as {existing['name']!r}")

        now = int(time.time())
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                position = conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM accounts"
                ).fetchone()[0]
                # An archived account comes back with its watermark intact, so it
                # resumes indexing rather than replaying its whole history.
                conn.execute(
                    "INSERT INTO accounts"
                    " (chain_id, address, name, position, active, created_at, is_owned, source)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?, 'ui')"
                    " ON CONFLICT (chain_id, address) DO UPDATE SET"
                    "   name = excluded.name, active = 1, is_owned = excluded.is_owned,"
                    "   source = 'ui'",
                    (chain_id, address, name, position, now, int(is_owned)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return next(
            account
            for account in self.list_accounts()
            if account.chain_id == chain_id and account.address == address
        )

    def remove_account(self, account_id: int) -> str | None:
        """Stop watching a UI-added account, keeping its indexed history.

        Config-declared accounts are not removable here: the file is their source
        of truth, and the next start would bring them straight back.

        The row is archived rather than deleted, so its activity stays queryable
        and re-adding the address resumes where it left off.
        """
        conn = self.connect()
        row = conn.execute(
            "SELECT name, source FROM accounts WHERE id = ? AND active = 1", (account_id,)
        ).fetchone()
        if row is None or row["source"] != "ui":
            return None
        with self._write_lock:
            conn.execute("UPDATE accounts SET active = 0 WHERE id = ?", (account_id,))
        return row["name"]

    def mark_synced(self, starts: dict[int, int], head: int) -> None:
        """Record the window each account has now been scanned over.

        The floor only ever moves down: having once read back to a block, that
        ground stays covered even if the configured window later shrinks.
        """
        if not starts:
            return
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "UPDATE accounts SET synced_to_block = ?,"
                    "  indexed_from_block = MIN(COALESCE(indexed_from_block, ?), ?)"
                    " WHERE id = ?",
                    [(head, start, start, account_id) for account_id, start in starts.items()],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def rename_account(self, account_id: int, name: str) -> str | None:
        return self._update_ui_account(account_id, "name = ?", (name,))

    def set_account_ownership(self, account_id: int, is_owned: bool) -> str | None:
        return self._update_ui_account(account_id, "is_owned = ?", (int(is_owned),))

    def _update_ui_account(self, account_id: int, assignment: str, values: tuple) -> str | None:
        """Edit an account the UI created. Config-declared accounts are refused:
        the file is their source of truth and would overwrite the change."""
        conn = self.connect()
        row = conn.execute(
            "SELECT name, source FROM accounts WHERE id = ? AND active = 1", (account_id,)
        ).fetchone()
        if row is None or row["source"] != "ui":
            return None
        with self._write_lock:
            conn.execute(
                f"UPDATE accounts SET {assignment} WHERE id = ?", (*values, account_id)
            )
        return row["name"]

    # ---------------------------------------------------------------- assets

    def get_or_create_asset(self, asset: AssetRef) -> int:
        conn = self.connect()
        row = conn.execute(
            "SELECT id, symbol, decimals FROM assets WHERE chain_id = ? AND contract_address = ?",
            (asset.chain_id, asset.contract_address),
        ).fetchone()
        if row is not None:
            # Late-arriving metadata (a token whose symbol we could not resolve
            # the first time) should upgrade the stored row.
            if asset.symbol and (row["symbol"] != asset.symbol or row["decimals"] != asset.decimals):
                if not row["symbol"] or row["decimals"] == 0:
                    conn.execute(
                        "UPDATE assets SET symbol = ?, decimals = ? WHERE id = ?",
                        (asset.symbol, asset.decimals, row["id"]),
                    )
            return row["id"]

        cursor = conn.execute(
            "INSERT INTO assets (chain_id, contract_address, symbol, decimals)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (chain_id, contract_address) DO UPDATE SET symbol = excluded.symbol"
            " RETURNING id",
            (asset.chain_id, asset.contract_address, asset.symbol, asset.decimals),
        )
        return cursor.fetchone()[0]

    def feed_assets(self) -> list[sqlite3.Row]:
        """Assets that actually appear in the feed, for the filter dropdown."""
        return list(
            self.connect().execute(
                "SELECT a.id, a.chain_id, a.contract_address, a.symbol, a.decimals,"
                "       COUNT(act.id) AS uses"
                " FROM assets a JOIN activities act ON act.asset_id = a.id"
                " GROUP BY a.id ORDER BY uses DESC, a.symbol"
            )
        )

    # ------------------------------------------------------------ activities

    def store_transfers(
        self,
        transfers: list[Transfer],
        account_index: dict[tuple[str, str], int],
    ) -> int:
        """Insert transfers idempotently. Returns the number of new rows.

        The unique key is ``(chain_id, event_key)``. Because the account columns
        are derived from the addresses rather than from which wallet's scan
        produced the row, the same transfer seen from both sides of a
        wallet-to-wallet transfer collapses to one identical row.
        """
        if not transfers:
            return 0

        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                before = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
                for transfer in transfers:
                    asset_id = self.get_or_create_asset(transfer.asset)
                    conn.execute(
                        "INSERT INTO activities ("
                        "  chain_id, event_key, tx_hash, block_number, block_timestamp,"
                        "  kind, asset_id, amount_raw, from_address, to_address,"
                        "  from_account_id, to_account_id"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (chain_id, event_key) DO UPDATE SET"
                        "   from_account_id = excluded.from_account_id,"
                        "   to_account_id   = excluded.to_account_id,"
                        "   block_number    = excluded.block_number,"
                        "   block_timestamp = excluded.block_timestamp",
                        (
                            transfer.chain_id,
                            transfer.event_key,
                            transfer.tx_hash,
                            transfer.block_number,
                            transfer.block_timestamp,
                            str(transfer.kind),
                            asset_id,
                            str(transfer.amount_raw),
                            transfer.from_address,
                            transfer.to_address,
                            account_index.get((transfer.chain_id, transfer.from_address)),
                            account_index.get((transfer.chain_id, transfer.to_address)),
                        ),
                    )
                inserted = (
                    conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] - before
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return inserted

    def known_event_keys(self, chain_id: str, from_block: int, to_block: int) -> set[str]:
        return {
            row[0]
            for row in self.connect().execute(
                "SELECT event_key FROM activities"
                " WHERE chain_id = ? AND block_number BETWEEN ? AND ?",
                (chain_id, from_block, to_block),
            )
        }

    def drop_activities(self, chain_id: str, event_keys: set[str]) -> int:
        """Remove activities that a re-scan no longer sees (reorged out)."""
        if not event_keys:
            return 0
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                total = 0
                keys = list(event_keys)
                for start in range(0, len(keys), 500):
                    chunk = keys[start : start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cursor = conn.execute(
                        f"DELETE FROM activities WHERE chain_id = ? AND event_key IN ({placeholders})",
                        (chain_id, *chunk),
                    )
                    total += cursor.rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return total

    def relink_accounts(self) -> None:
        """Recompute the account columns on every activity from the current
        accounts table, so history indexed before a wallet was added is still
        attributed to it (and history of a removed wallet is released)."""
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE activities SET"
                    "  from_account_id = (SELECT id FROM accounts"
                    "     WHERE accounts.chain_id = activities.chain_id"
                    "       AND accounts.address = activities.from_address),"
                    "  to_account_id = (SELECT id FROM accounts"
                    "     WHERE accounts.chain_id = activities.chain_id"
                    "       AND accounts.address = activities.to_address)"
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def activity_count(self) -> int:
        return self.connect().execute("SELECT COUNT(*) FROM activities").fetchone()[0]

    # ---------------------------------------------------------------- feed

    def _activity_filters(
        self,
        *,
        account_ids: list[int] | None,
        direction: Direction,
        asset_id: int | None,
        search: str,
        dust_thresholds: dict[int, float] | None = None,
        exclude_assets: set[int] | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        """WHERE clauses shared by the feed page and its count."""
        where: list[str] = []
        params: dict[str, object] = {}

        if exclude_assets:
            # Not `asset_id` as the loop variable: that is the asset *filter*
            # parameter, and shadowing it silently narrowed the whole feed to
            # whichever asset happened to be excluded last.
            names = []
            for index, skipped in enumerate(sorted(exclude_assets)):
                params[f"skip{index}"] = skipped
                names.append(f":skip{index}")
            where.append(f"a.asset_id NOT IN ({', '.join(names)})")

        # Dust is excluded here rather than after the fact: filtering a page in
        # Python would leave short pages and a cursor that skips rows.
        if dust_thresholds:
            cases, ids = [], []
            for index, (asset, ceiling) in enumerate(dust_thresholds.items()):
                params[f"dustid{index}"] = asset
                params[f"dustmax{index}"] = ceiling
                cases.append(f"WHEN :dustid{index} THEN :dustmax{index}")
                ids.append(f":dustid{index}")
            where.append(
                f"NOT (a.asset_id IN ({', '.join(ids)})"
                f" AND CAST(a.amount_raw AS REAL) < CASE a.asset_id {' '.join(cases)} END)"
            )

        if account_ids is not None:
            if not account_ids:
                return ["1 = 0"], params
            names = []
            for index, account_id in enumerate(account_ids):
                key = f"acct{index}"
                params[key] = account_id
                names.append(f":{key}")
            scope = ", ".join(names)
            if direction is Direction.IN:
                where.append(f"a.to_account_id IN ({scope})")
            elif direction is Direction.OUT:
                where.append(f"a.from_account_id IN ({scope})")
            else:
                where.append(
                    f"(a.from_account_id IN ({scope}) OR a.to_account_id IN ({scope}))"
                )
        elif direction is Direction.IN:
            where.append("a.to_account_id IS NOT NULL")
        elif direction is Direction.OUT:
            where.append("a.from_account_id IS NOT NULL")

        if asset_id is not None:
            where.append("a.asset_id = :asset_id")
            params["asset_id"] = asset_id

        if term := search.strip().lower():
            params["like"] = f"%{term}%"
            where.append(
                "(a.tx_hash LIKE :like OR a.from_address LIKE :like OR a.to_address LIKE :like"
                " OR ast.symbol LIKE :like OR fa.name LIKE :like OR ta.name LIKE :like"
                " OR fl.label LIKE :like OR tl.label LIKE :like)"
            )

        return where, params

    def query_activities(
        self,
        *,
        account_ids: list[int] | None = None,
        direction: Direction = Direction.ALL,
        asset_id: int | None = None,
        search: str = "",
        dust_thresholds: dict[int, float] | None = None,
        exclude_assets: set[int] | None = None,
        cursor: tuple[int, int] | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        """``account_ids`` is the scope: one account, the owned ones, or None
        for every monitored account."""
        where, params = self._activity_filters(
            account_ids=account_ids, direction=direction, asset_id=asset_id, search=search,
            dust_thresholds=dust_thresholds, exclude_assets=exclude_assets,
        )
        params["limit"] = limit + 1

        if cursor is not None:
            params["cursor_ts"], params["cursor_id"] = cursor
            where.append("(a.block_timestamp, a.id) < (:cursor_ts, :cursor_id)")

        sql = (
            self._ACTIVITY_SELECT
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY a.block_timestamp DESC, a.id DESC LIMIT :limit"
        )
        return list(self.connect().execute(sql, params))

    def count_activities(
        self,
        *,
        account_ids: list[int] | None = None,
        direction: Direction = Direction.ALL,
        asset_id: int | None = None,
        search: str = "",
        dust_thresholds: dict[int, float] | None = None,
        exclude_assets: set[int] | None = None,
    ) -> int:
        """How many activities the current filters match, for "N transactions"."""
        where, params = self._activity_filters(
            account_ids=account_ids, direction=direction, asset_id=asset_id, search=search,
            dust_thresholds=dust_thresholds, exclude_assets=exclude_assets,
        )
        sql = (
            "SELECT COUNT(*) FROM activities a"
            " JOIN assets ast ON ast.id = a.asset_id"
            " LEFT JOIN accounts fa ON fa.id = a.from_account_id"
            " LEFT JOIN accounts ta ON ta.id = a.to_account_id"
            " LEFT JOIN labels fl ON fl.chain_id = a.chain_id AND fl.address = a.from_address"
            " LEFT JOIN labels tl ON tl.chain_id = a.chain_id AND tl.address = a.to_address"
            + (" WHERE " + " AND ".join(where) if where else "")
        )
        return self.connect().execute(sql, params).fetchone()[0]

    def all_assets(self) -> list[sqlite3.Row]:
        return list(
            self.connect().execute(
                "SELECT id, chain_id, contract_address, symbol, decimals FROM assets"
            )
        )

    def sent_asset_ids(self) -> set[int]:
        """Assets a monitored account has itself sent.

        Spam only ever arrives. Having sent something is the strongest signal
        available here that the holder considers it real, and it costs one
        indexed query rather than a third-party list.
        """
        return {
            row[0]
            for row in self.connect().execute(
                "SELECT DISTINCT asset_id FROM activities WHERE from_account_id IS NOT NULL"
            )
        }

    def get_activity(self, activity_id: int) -> sqlite3.Row | None:
        rows = self.connect().execute(
            self._ACTIVITY_SELECT + " WHERE a.id = :id", {"id": activity_id}
        ).fetchall()
        return rows[0] if rows else None

    def transfers_in_tx(self, chain_id: str, tx_hash: str) -> list[sqlite3.Row]:
        """Every movement one transaction produced, in on-chain order.

        A swap is one transaction and four transfers, and reading them as four
        unrelated rows minutes apart in the feed is how the feed lies. They are
        kept as separate rows — the amounts are not ours to add up — but they
        are shown together.
        """
        return list(
            self.connect().execute(
                self._ACTIVITY_SELECT
                + " WHERE a.chain_id = :chain AND a.tx_hash = :tx"
                + " ORDER BY a.id",
                {"chain": chain_id, "tx": tx_hash.lower()},
            )
        )

    # ------------------------------------------------------------- balances

    def replace_balances(self, account_id: int, balances: list[Balance]) -> None:
        conn = self.connect()
        now = int(time.time())
        rows = [(account_id, self.get_or_create_asset(b.asset), str(b.amount_raw), now) for b in balances]
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM balances WHERE account_id = ?", (account_id,))
                conn.executemany(
                    "INSERT INTO balances (account_id, asset_id, amount_raw, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def balances_by_account(self) -> dict[int, list[sqlite3.Row]]:
        """Balances per account: native first, then tokens seen in the feed,
        then the rest alphabetically."""
        rows = self.connect().execute(
            """
            SELECT b.account_id, b.amount_raw, b.updated_at,
                   ast.id AS asset_id, ast.chain_id, ast.contract_address,
                   ast.symbol, ast.decimals,
                   EXISTS (
                       SELECT 1 FROM activities act
                       WHERE act.asset_id = ast.id
                         AND (act.from_account_id = b.account_id
                              OR act.to_account_id = b.account_id)
                   ) AS seen_in_feed
            FROM balances b
            JOIN assets ast ON ast.id = b.asset_id
            ORDER BY b.account_id,
                     ast.contract_address <> '' ,
                     seen_in_feed DESC,
                     ast.symbol
            """
        )
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["account_id"], []).append(row)
        return grouped

    # --------------------------------------------------------------- prices

    def replace_prices(self, prices: dict[int, str], currency: str) -> None:
        """Store spot prices keyed by asset id. Assets absent from ``prices``
        keep their previous row: a provider that stops quoting one token should
        not blank out every other valuation."""
        if not prices:
            return
        conn = self.connect()
        now = int(time.time())
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "INSERT INTO asset_prices (asset_id, currency, price, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (asset_id) DO UPDATE SET"
                    "   currency = excluded.currency,"
                    "   price = excluded.price,"
                    "   updated_at = excluded.updated_at",
                    [(asset_id, currency, price, now) for asset_id, price in prices.items()],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def prices_by_asset(self) -> dict[int, sqlite3.Row]:
        return {
            row["asset_id"]: row
            for row in self.connect().execute(
                "SELECT asset_id, currency, price, updated_at FROM asset_prices"
            )
        }

    def native_asset_id(self, chain_id: str) -> int | None:
        row = self.connect().execute(
            "SELECT id FROM assets WHERE chain_id = ? AND contract_address = ''",
            (chain_id,),
        ).fetchone()
        return None if row is None else row["id"]

    def held_assets(self) -> list[sqlite3.Row]:
        """Assets any monitored account currently holds — the set worth pricing."""
        return list(
            self.connect().execute(
                "SELECT DISTINCT a.id, a.chain_id, a.contract_address, a.symbol, a.decimals"
                " FROM assets a"
                " JOIN balances b ON b.asset_id = a.id"
                " JOIN accounts acc ON acc.id = b.account_id AND acc.active = 1"
                " WHERE CAST(b.amount_raw AS REAL) > 0"
            )
        )

    # ----------------------------------------------------------- sync state

    def get_sync_status(self, chain_id: str) -> SyncStatus:
        row = self.connect().execute(
            "SELECT * FROM sync_state WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if row is None:
            return SyncStatus(chain_id=chain_id, activity_count=self.activity_count())
        return SyncStatus(
            chain_id=row["chain_id"],
            last_synced_block=row["last_synced_block"],
            head_block=row["head_block"],
            last_success_at=row["last_success_at"],
            last_attempt_at=row["last_attempt_at"],
            last_error=row["last_error"],
            backfill_done=bool(row["backfill_done"]),
            requested_from_block=row["requested_from_block"],
            activity_count=self.activity_count(),
        )

    def request_history_from(self, chain_id: str, block: int) -> None:
        """Ask the indexer to reach back to ``block``. Only ever deeper."""
        conn = self.connect()
        with self._write_lock:
            conn.execute(
                "INSERT INTO sync_state (chain_id, requested_from_block) VALUES (?, ?)"
                " ON CONFLICT (chain_id) DO UPDATE SET requested_from_block ="
                "   MIN(COALESCE(requested_from_block, ?), ?)",
                (chain_id, block, block, block),
            )

    def history_floor(self, chain_id: str) -> int | None:
        """The earliest block any active account has been read from."""
        row = self.connect().execute(
            "SELECT MIN(indexed_from_block) FROM accounts"
            " WHERE active = 1 AND chain_id = ? AND indexed_from_block IS NOT NULL",
            (chain_id,),
        ).fetchone()
        return row[0] if row else None

    def record_attempt(self, chain_id: str) -> None:
        self._upsert_sync(chain_id, {"last_attempt_at": int(time.time())})

    def record_success(self, chain_id: str, *, last_synced_block: int, head_block: int) -> None:
        self._upsert_sync(
            chain_id,
            {
                "last_synced_block": last_synced_block,
                "head_block": head_block,
                "last_success_at": int(time.time()),
                "last_error": None,
                "backfill_done": 1,
            },
        )

    def record_failure(self, chain_id: str, error: str) -> None:
        self._upsert_sync(chain_id, {"last_error": error[:500]})

    def _upsert_sync(self, chain_id: str, values: dict[str, object]) -> None:
        conn = self.connect()
        columns = ", ".join(values)
        placeholders = ", ".join("?" * len(values))
        updates = ", ".join(f"{column} = excluded.{column}" for column in values)
        with self._write_lock:
            conn.execute(
                f"INSERT INTO sync_state (chain_id, {columns}) VALUES (?, {placeholders})"
                f" ON CONFLICT (chain_id) DO UPDATE SET {updates}",
                (chain_id, *values.values()),
            )
