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
from app.redact import redact

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_RE = re.compile(r"^(\d+)_")


# An event is part of what the app shows when at least one end is a watched
# account. The account columns only ever name *active* accounts (see
# relink_accounts), so this one test hides a removed wallet's history without
# deleting any of it.
_VISIBLE = "(a.from_account_id IS NOT NULL OR a.to_account_id IS NOT NULL)"


# The active account with a given (chain, address), or NULL.
_ACTIVE_ACCOUNT = (
    "(SELECT id FROM accounts WHERE chain_id = ? AND address = ? AND active = 1)"
)


def _decimals(alias: str) -> str:
    """The believed decimals of an asset, NULL when unknown. Every read goes
    through this, so no caller can mistake "unknown" for zero."""
    return f"CASE WHEN {alias}.decimals_known THEN {alias}.decimals END AS decimals"


class Database:
    """Owns the SQLite file. Connections are per-thread; FastAPI runs sync
    endpoints in a threadpool and the indexer writes from another thread."""

    # One projection for the feed and for a single activity, so a row rendered
    # in the list and the same row opened on its own cannot drift apart.
    _ACTIVITY_SELECT = """
        SELECT a.id, a.chain_id, a.tx_hash, a.block_number, a.block_timestamp, a.kind,
               a.amount_raw, a.from_address, a.to_address,
               a.from_account_id, a.to_account_id,
               a.asset_id, ast.contract_address, ast.symbol, {decimals},
               fa.name AS from_account_name, ta.name AS to_account_name,
               fl.label AS from_label, tl.label AS to_label
        FROM activities a
        JOIN assets ast ON ast.id = a.asset_id
        LEFT JOIN accounts fa ON fa.id = a.from_account_id
        LEFT JOIN accounts ta ON ta.id = a.to_account_id
        LEFT JOIN labels fl ON fl.chain_id = a.chain_id AND fl.address = a.from_address
        LEFT JOIN labels tl ON tl.chain_id = a.chain_id AND tl.address = a.to_address
    """.format(decimals=_decimals("ast"))

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

        # A wallet dropped from the file loses its history from every view, one
        # put back regains it — before the first page is served.
        self.relink_accounts()
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

        # History already indexed — through another wallet, or before this one
        # was removed — is attributed to it again; the next cycle resumes from
        # its own watermark for the rest.
        self.relink_accounts()
        return next(
            account
            for account in self.list_accounts()
            if account.chain_id == chain_id and account.address == address
        )

    def remove_account(self, account_id: int) -> str | None:
        """Stop watching a UI-added account, keeping its indexed history.

        Config-declared accounts are not removable here: the file is their source
        of truth, and the next start would bring them straight back.

        The row is archived rather than deleted, and so is its history: every
        event stays in the table, unattributed, and re-adding the address
        attributes it again and resumes indexing where it left off.
        """
        conn = self.connect()
        row = conn.execute(
            "SELECT name, source FROM accounts WHERE id = ? AND active = 1", (account_id,)
        ).fetchone()
        if row is None or row["source"] != "ui":
            return None
        with self._write_lock:
            conn.execute("UPDATE accounts SET active = 0 WHERE id = ?", (account_id,))
        self.relink_accounts()
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
        """The id of an asset, created on first sight.

        Decimals only ever move from unknown to known — a later failed lookup
        never downgrades a value already learned, and a value learned later
        replaces an unknown one, together with the stub symbol that came with it.
        """
        conn = self.connect()
        known = asset.decimals is not None
        row = conn.execute(
            "SELECT id, symbol, decimals_known FROM assets"
            " WHERE chain_id = ? AND contract_address = ?",
            (asset.chain_id, asset.contract_address),
        ).fetchone()
        if row is not None:
            if known and not row["decimals_known"]:
                conn.execute(
                    "UPDATE assets SET symbol = ?, decimals = ?, decimals_known = 1 WHERE id = ?",
                    (asset.symbol or row["symbol"], asset.decimals, row["id"]),
                )
            elif asset.symbol and not row["symbol"]:
                conn.execute("UPDATE assets SET symbol = ? WHERE id = ?", (asset.symbol, row["id"]))
            return row["id"]

        cursor = conn.execute(
            "INSERT INTO assets (chain_id, contract_address, symbol, decimals, decimals_known)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (chain_id, contract_address) DO UPDATE SET symbol = excluded.symbol"
            " RETURNING id",
            (
                asset.chain_id,
                asset.contract_address,
                asset.symbol,
                asset.decimals if known else 0,
                int(known),
            ),
        )
        return cursor.fetchone()[0]

    def adopt_listed_decimals(self, chain_id: str, listed) -> int:
        """Correct stored decimals from a reviewed list, ``listed(contract)``
        returning the list's value or ``None``. Run at startup, so a wrong
        value left by an older version does not wait for the next lookup —
        or last forever for a token no longer held."""
        conn = self.connect()
        fixes = []
        for row in conn.execute(
            "SELECT id, contract_address, decimals, decimals_known FROM assets"
            " WHERE chain_id = ? AND contract_address <> ''",
            (chain_id,),
        ):
            value = listed(row["contract_address"])
            if value is not None and (not row["decimals_known"] or row["decimals"] != value):
                fixes.append((value, row["id"]))
        if fixes:
            with self._write_lock:
                conn.executemany(
                    "UPDATE assets SET decimals = ?, decimals_known = 1 WHERE id = ?", fixes
                )
            log.info("took decimals for %d asset(s) from the token list", len(fixes))
        return len(fixes)

    def feed_assets(self) -> list[sqlite3.Row]:
        """Assets that actually appear in the feed, for the filter dropdown."""
        return list(
            self.connect().execute(
                f"SELECT a.id, a.chain_id, a.contract_address, a.symbol, {_decimals('a')},"
                "       COUNT(act.id) AS uses"
                " FROM assets a JOIN activities act ON act.asset_id = a.id"
                " WHERE act.from_account_id IS NOT NULL OR act.to_account_id IS NOT NULL"
                " GROUP BY a.id ORDER BY uses DESC, a.symbol"
            )
        )

    # ------------------------------------------------------------ activities

    def store_transfers(self, transfers: list[Transfer]) -> int:
        """Insert transfers idempotently. Returns the number of new rows.

        The unique key is ``(chain_id, event_key)``. Because the account columns
        are derived from the addresses rather than from which wallet's scan
        produced the row, the same transfer seen from both sides of a
        wallet-to-wallet transfer collapses to one identical row.

        They are looked up here, against the accounts that are active *now*:
        a cycle that started before a wallet was removed must not attribute
        what it stores to that wallet.
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
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                        f"  {_ACTIVE_ACCOUNT}, {_ACTIVE_ACCOUNT})"
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
                            transfer.chain_id,
                            transfer.from_address,
                            transfer.chain_id,
                            transfer.to_address,
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

    def prunable_event_keys(
        self, chain_id: str, windows: dict[int, int], head: int
    ) -> set[str]:
        """Events some active wallet was responsible for re-checking this cycle.

        ``windows`` maps an account id to the first block of its reorg window;
        each window runs to ``head``. An event qualifies when one of its ends
        is such an account and its block lies inside that account's window.
        One indexed range scan over the union of the windows, and the
        per-account test in SQL — not a query per wallet.

        The account columns only ever name active accounts (relink_accounts),
        so a removed wallet's history is never a candidate: it was not scanned.
        """
        if not windows:
            return set()
        values = ", ".join("(?, ?)" for _ in windows)
        params: list[int | str] = [v for pair in windows.items() for v in pair]
        params += [chain_id, min(windows.values()), head]
        return {
            row[0]
            for row in self.connect().execute(
                f"WITH win(account_id, from_block) AS (VALUES {values})"
                " SELECT a.event_key FROM activities a"
                " WHERE a.chain_id = ? AND a.block_number BETWEEN ? AND ?"
                "   AND EXISTS (SELECT 1 FROM win"
                "     WHERE a.block_number >= win.from_block"
                "       AND win.account_id IN (a.from_account_id, a.to_account_id))",
                params,
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
        """Point the account columns of every activity at the *active* account
        with that address, or at nothing.

        This is the one invariant the whole read side relies on: an inactive
        account is never named on an activity. Its history stays in the table
        untouched, simply unattributed — so the feed, IN/OUT, the pair
        collapse and "sent by one of your wallets" all forget it at once, and
        re-adding the address attributes it again.

        Written as four targeted updates rather than one rewrite of every row:
        a restart, where nothing changed, touches no rows at all.
        """
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                for column, address in (("from_account_id", "from_address"),
                                        ("to_account_id", "to_address")):
                    # Release what an inactive account was holding.
                    conn.execute(
                        f"UPDATE activities SET {column} = NULL WHERE {column} IN"
                        " (SELECT id FROM accounts WHERE active = 0)"
                    )
                    # Attribute what an active account has not been given yet.
                    conn.execute(
                        f"UPDATE activities SET {column} = ("
                        "   SELECT acc.id FROM accounts acc"
                        "   WHERE acc.chain_id = activities.chain_id"
                        f"    AND acc.address = activities.{address} AND acc.active = 1)"
                        f" WHERE {column} IS NULL AND EXISTS ("
                        "   SELECT 1 FROM accounts acc"
                        "   WHERE acc.chain_id = activities.chain_id"
                        f"    AND acc.address = activities.{address} AND acc.active = 1)"
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
        else:
            # Everything monitored means everything an active account is part
            # of — not every row ever indexed.
            where.append(_VISIBLE)

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
                f"SELECT id, chain_id, contract_address, symbol, {_decimals('assets')}"
                " FROM assets"
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
            self._ACTIVITY_SELECT + f" WHERE a.id = :id AND {_VISIBLE}", {"id": activity_id}
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
                + f" AND {_VISIBLE}"
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
                   ast.symbol, {decimals},
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
            """.format(decimals=_decimals("ast"))
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
                f"SELECT DISTINCT a.id, a.chain_id, a.contract_address, a.symbol, {_decimals('a')}"
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
            # Redacted on the way out as well as on the way in: a database
            # written by an older version can still hold an unscrubbed message.
            last_error=redact(row["last_error"]),
            backfill_done=bool(row["backfill_done"]),
            requested_from_block=row["requested_from_block"],
            activity_count=self.activity_count(),
            history_status=row["history_status"],
            history_target=row["history_target"],
            history_error=redact(row["history_error"]),
            balances_error=redact(row["balances_error"]),
            balances_failed_at=row["balances_failed_at"],
            balances_ok_at=row["balances_ok_at"],
        )

    # ------------------------------------------------- older-history requests
    #
    # "Load N more days" is a request the page makes and the background
    # indexer carries out. The rule for repeated requests, in one place:
    #
    #   queued or running  -> nothing changes; the request already in hand
    #                         is the answer (ten clicks are one request)
    #   failed             -> queued again for the same block, not a deeper one
    #   completed or none  -> a new request, one window deeper than the
    #                         deepest point reached or asked for
    #
    # The block asked for is also written to requested_from_block, which the
    # indexer treats as a floor for every cycle from then on, so the request
    # survives a restart and is retried until it succeeds.

    HISTORY_ACTIVE = ("queued", "running")

    def request_history(self, chain_id: str, window_blocks: int) -> SyncStatus:
        conn = self.connect()
        now = int(time.time())
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sync_state WHERE chain_id = ?", (chain_id,)
                ).fetchone()
                status = row["history_status"] if row else None
                if status not in self.HISTORY_ACTIVE:
                    if status == "failed" and row["history_target"] is not None:
                        target = row["history_target"]
                    else:
                        floor = self._history_floor(conn, chain_id)
                        requested = row["requested_from_block"] if row else None
                        known = [x for x in (floor, requested) if x is not None]
                        reference = min(known) if known else ((row and row["head_block"]) or 0)
                        target = max(0, reference - window_blocks)
                    conn.execute(
                        "INSERT INTO sync_state (chain_id) VALUES (?)"
                        " ON CONFLICT (chain_id) DO NOTHING",
                        (chain_id,),
                    )
                    conn.execute(
                        "UPDATE sync_state SET"
                        "  requested_from_block = MIN(COALESCE(requested_from_block, ?), ?),"
                        "  history_status = 'queued', history_target = ?,"
                        "  history_requested_at = ?, history_finished_at = NULL,"
                        "  history_error = NULL"
                        " WHERE chain_id = ?",
                        (target, target, target, now, chain_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_sync_status(chain_id)

    def history_started(self, chain_id: str) -> None:
        """A cycle is now reaching for the requested history."""
        self._history_transition(chain_id, "running", ("queued", "failed"))

    def history_finished(self, chain_id: str, error: str | None = None) -> None:
        """The cycle that was reaching for it ended — well or not."""
        conn = self.connect()
        with self._write_lock:
            conn.execute(
                "UPDATE sync_state SET history_status = ?, history_finished_at = ?,"
                " history_error = ? WHERE chain_id = ? AND history_status = 'running'",
                (
                    "failed" if error else "completed",
                    int(time.time()),
                    (redact(error) or "")[:500] if error else None,
                    chain_id,
                ),
            )

    def resume_history_after_restart(self, chain_id: str) -> None:
        """A request that was running when the process stopped is queued again;
        its block is still in requested_from_block, so nothing is lost."""
        self._history_transition(chain_id, "queued", ("running",))

    def _history_transition(self, chain_id: str, to: str, from_: tuple[str, ...]) -> None:
        conn = self.connect()
        marks = ", ".join("?" * len(from_))
        with self._write_lock:
            conn.execute(
                f"UPDATE sync_state SET history_status = ? WHERE chain_id = ?"
                f" AND history_status IN ({marks})",
                (to, chain_id, *from_),
            )

    # ------------------------------------------------------------- balances

    def record_balances(self, chain_id: str, error: str | None) -> None:
        """Balances succeeded, or why they did not — apart from indexing,
        whose watermark does not depend on them."""
        now = int(time.time())
        if error:
            self._upsert_sync(
                chain_id,
                {"balances_error": (redact(error) or "")[:500], "balances_failed_at": now},
            )
        else:
            self._upsert_sync(chain_id, {"balances_error": None, "balances_ok_at": now})

    def history_floor(self, chain_id: str) -> int | None:
        """The earliest block any active account has been read from."""
        return self._history_floor(self.connect(), chain_id)

    @staticmethod
    def _history_floor(conn: sqlite3.Connection, chain_id: str) -> int | None:
        row = conn.execute(
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
        # Every error the UI and the JSON API will ever show passes through
        # here, so this is where it is scrubbed. Before truncating: a cut in
        # the middle of a key would leave half of it unrecognisable.
        self._upsert_sync(chain_id, {"last_error": (redact(error) or "")[:500]})

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
