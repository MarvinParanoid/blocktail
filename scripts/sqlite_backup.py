#!/usr/bin/env python3
"""Consistent backups of blocktail's SQLite database, and the restore that goes
with them.

Runs inside the application image (it needs nothing but the standard library,
and the image has no ``sqlite3`` binary), as the same unprivileged user as the
app, so a file it creates in the data volume is one the app can still open.

Why not ``cp``: the database runs in WAL mode, so recent writes live in
``blocktail.db-wal`` until a checkpoint moves them into the main file. Copying
the main file while the app is running loses them, or catches a page
half-written. SQLite's online backup API copies a consistent snapshot through
the database's own locking, with the app still running.

    backup   --source DB --dest-dir DIR --label TEXT [--keep N] [--missing-ok]
    verify   --path FILE
    restore  --backup FILE --target DB [--discard-wal]

A backup is written under a temporary name, checked with ``integrity_check``,
converted to a single self-contained file (journal_mode=DELETE), and only then
renamed into place. Older backups are pruned only after that succeeded, so a
failed run never costs the last good one.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

PREFIX = "blocktail-"
SUFFIX = ".db"


class BackupError(Exception):
    pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _remove_quietly(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def check(path: Path) -> dict:
    """Open a backup on its own and prove it is a usable blocktail database."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise BackupError(f"integrity_check failed for {path}: {'; '.join(result[:5])}")
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "schema_migrations" not in tables:
            raise BackupError(f"{path} is not a blocktail database (no schema_migrations)")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        activities = (
            conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
            if "activities" in tables
            else 0
        )
        return {"schema_version": version, "activities": activities}
    finally:
        conn.close()


def backup(source: Path, dest_dir: Path, label: str, keep: int, missing_ok: bool) -> Path | None:
    if not source.is_file():
        if missing_ok:
            return None
        raise BackupError(f"no database at {source}")
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe_label = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:40]
    final = dest_dir / f"{PREFIX}{stamp}-{safe_label}{SUFFIX}"
    partial = dest_dir / f".partial-{final.name}"
    _remove_quietly(partial, Path(f"{partial}-journal"))

    try:
        src = sqlite3.connect(source, timeout=30)
        try:
            dst = sqlite3.connect(partial)
            try:
                src.backup(dst)
                # One self-contained file: a restore should not depend on a
                # -wal sitting beside it.
                dst.execute("PRAGMA journal_mode = DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        facts = check(partial)
        with open(partial, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, final)
        _fsync_dir(dest_dir)
    except BaseException:
        _remove_quietly(partial, Path(f"{partial}-journal"))
        raise

    # Only now, with a verified new backup in place, is anything old removed.
    backups = sorted(p for p in dest_dir.glob(f"{PREFIX}*{SUFFIX}") if p.is_file())
    for old in backups[:-keep]:
        _remove_quietly(old)
    print(
        f"backup ok: {final.name} (schema {facts['schema_version']},"
        f" {facts['activities']} activities)",
        file=sys.stderr,
    )
    return final


def restore(backup_file: Path, target: Path, discard_wal: bool) -> Path:
    """Replace the database with a backup. The app must be stopped first."""
    if not backup_file.is_file():
        raise BackupError(f"no backup at {backup_file}")
    facts = check(backup_file)

    wal, shm = Path(f"{target}-wal"), Path(f"{target}-shm")
    pending = wal.exists() and wal.stat().st_size > 0
    if pending and not discard_wal:
        raise BackupError(
            f"{wal} holds unwritten changes: the app may still be running. Stop it"
            " first; if it is stopped and you mean to throw those changes away,"
            " pass --discard-wal"
        )

    staging = target.with_name(f".restore-{target.name}")
    _remove_quietly(staging)
    shutil.copyfile(backup_file, staging)
    check(staging)

    kept = None
    if target.exists():
        kept = target.with_name(
            f"{target.name}.before-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        )
        os.replace(target, kept)
    _remove_quietly(wal, shm)
    os.replace(staging, target)
    _fsync_dir(target.parent)
    print(
        f"restored {backup_file.name} -> {target} (schema {facts['schema_version']},"
        f" {facts['activities']} activities)"
        + (f"; the previous file is kept as {kept.name}" if kept else ""),
        file=sys.stderr,
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup")
    b.add_argument("--source", type=Path, required=True)
    b.add_argument("--dest-dir", type=Path, required=True)
    b.add_argument("--label", default="manual")
    b.add_argument("--keep", type=int, default=5)
    b.add_argument("--missing-ok", action="store_true",
                   help="exit 0 and print 'none' when there is no database yet")

    v = sub.add_parser("verify")
    v.add_argument("--path", type=Path, required=True)

    r = sub.add_parser("restore")
    r.add_argument("--backup", type=Path, required=True)
    r.add_argument("--target", type=Path, required=True)
    r.add_argument("--discard-wal", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            made = backup(args.source, args.dest_dir, args.label, args.keep, args.missing_ok)
            # stdout carries exactly one token for the deploy script to read.
            print(made.name if made else "none")
        elif args.command == "verify":
            facts = check(args.path)
            print(f"ok: schema {facts['schema_version']}, {facts['activities']} activities")
        else:
            restore(args.backup, args.target, args.discard_wal)
    except (BackupError, sqlite3.Error, OSError) as exc:
        print(f"sqlite_backup: {args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
