"""A rollback to the previous release works on the database this one leaves.

`remote-deploy.sh rollback` starts the previous image and does NOT restore the
database: the previous code must read and write a file this release has
migrated. This runs both for real, each in its own interpreter, on one
synthetic database (tests/rollback_phases.py):

1. the previous release fills it: wallets from the config and one added and
   renamed from the page, transfers of ETH, USDC, an internal one and a token
   whose decimals are unknown; and snapshots every read;
2. this release migrates it and runs, asking for older history, so the new
   columns hold what a running release would leave;
3. the previous release again: every read must answer as before (the history
   floor aside, which step 2 moved), and adding, renaming and removing a
   wallet and asking for older history must work, with no SQLite error;
4. this release again, on what the previous one wrote: rolling forward.

The previous release is a git ref, BLOCKTAIL_ROLLBACK_BASE: in CI the release
production runs, as scripts/rollback-base.sh names it (the newest release tag
HEAD contains) — never "the previous commit". Without it the test is skipped;
the CI job sets BLOCKTAIL_ROLLBACK_REQUIRED=1, so there a missing or unknown
ref fails instead. When the base is the very commit under test (HEAD is the
release) there is no rollback to check, and the test says so by skipping.

BLOCKTAIL_ROLLBACK_NEW names the release under test when it is not HEAD — for
a transition fixed in history, such as v0.1.0 (c9a97aa) → v0.2.0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PHASES = ROOT / "tests" / "rollback_phases.py"
BASE = os.environ.get("BLOCKTAIL_ROLLBACK_BASE", "")
NEW = os.environ.get("BLOCKTAIL_ROLLBACK_NEW", "")
REQUIRED = os.environ.get("BLOCKTAIL_ROLLBACK_REQUIRED") == "1"


def _commit(ref: str) -> str | None:
    done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet",
                           f"{ref}^{{commit}}"], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def _unpack(ref: str, target: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref],
                             capture_output=True, check=False)
    assert archive.returncode == 0, archive.stderr.decode()
    target.mkdir()
    with tarfile.open(fileobj=BytesIO(archive.stdout)) as tar:
        tar.extractall(target, filter="data")
    return target


def _releases(tmp_path: Path) -> tuple[Path, Path]:
    """(the release rolled back to, the release under test)."""
    if not BASE:
        if REQUIRED:
            pytest.fail("BLOCKTAIL_ROLLBACK_REQUIRED=1 but BLOCKTAIL_ROLLBACK_BASE is not set")
        pytest.skip("set BLOCKTAIL_ROLLBACK_BASE to the release production runs"
                    " (scripts/rollback-base.sh)")
    base = _commit(BASE)
    new = _commit(NEW or "HEAD")
    if base is None or new is None:
        message = f"cannot resolve {BASE if base is None else NEW}"
        if REQUIRED:
            pytest.fail(message)
        pytest.skip(message)
    if base == new:
        pytest.skip(f"the base {BASE} is the release under test ({new[:12]}):"
                    " there is no rollback to check")
    previous = _unpack(base, tmp_path / "previous")
    current = _unpack(new, tmp_path / "current") if NEW else ROOT
    return previous, current


# What the release under test does on purpose before the previous one comes
# back: it asks for older history. Any version that knows that feature shows
# the result — the index reaching further back, and the request's state in
# /api/status — so those, and only those, may differ.
def _expected_difference(path: str, before: str, after: str) -> bool:
    if path.startswith("/api/"):
        # The snapshots stand a placeholder for every time (<ts>, <age>…);
        # bare ones are quoted so the body parses as JSON again.
        quote = re.compile(r"(?<=[:\[,])<(\w+)>")
        try:
            old, new = (json.loads(quote.sub(r'"<\1>"', body)) for body in (before, after))
        except ValueError:
            return False
        if isinstance(old, dict) and isinstance(new, dict):
            old.pop("history", None)
            new.pop("history", None)
            return old == new
        return False
    old = [line for line in before.splitlines() if line not in after]
    new = [line for line in after.splitlines() if line not in before]
    return all("indexed back to block" in line for line in old + new)


def _phase(root: Path, phase: str, work: Path) -> dict:
    done = subprocess.run([sys.executable, "-I", str(PHASES), str(root), phase, str(work)],
                          capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 0, f"{phase} crashed:\n{done.stdout}\n{done.stderr}"
    report = json.loads(done.stdout)
    assert report["problems"] == [], f"{phase}: {report['problems']}"
    assert report["schema"]["integrity"] == "ok", f"{phase}: {report['schema']}"
    assert report["schema"]["fk_violations"] == 0, f"{phase}: {report['schema']}"
    return report


def test_the_previous_release_runs_on_this_releases_database(tmp_path):
    previous, current = _releases(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    ours = sorted(int(p.name[:3]) for p in (current / "app" / "migrations").glob("[0-9]*.sql"))

    before = _phase(previous, "old1", work)
    migrated = _phase(current, "new", work)
    assert migrated["schema"]["versions"] == ours, migrated["schema"]["versions"]
    # A release without a migration is fine; then this shows only that the
    # previous release runs on its own schema after this one has used it.
    assert set(before["schema"]["versions"]) <= set(ours)

    back = _phase(previous, "old2", work)
    # Reads answer as before, except for what the release under test was
    # asked to do (_expected_difference).
    snap6 = json.loads((work / "snap6.json").read_text())
    snap8 = json.loads((work / "snap8.json").read_text())
    assert back["reads_compared"] >= 20
    for path in back["reads_differing"]:
        assert snap6[path][0] == snap8[path][0], f"{path}: status changed"
        assert _expected_difference(path, snap6[path][1], snap8[path][1]), \
            f"{path} differs after the migration:\n{snap6[path][1][:600]}\n---\n{snap8[path][1][:600]}"
    # Writes on the migrated file: a wallet added and renamed (its transfer
    # indexed), another removed.
    assert back["later_rows"] >= 1
    assert "Later renamed" in back["accounts"] and "Trading desk" not in back["accounts"]
    assert back["schema"]["versions"] == ours

    forward = _phase(current, "new2", work)
    assert "Later renamed" in forward["accounts"] and "Trading desk" not in forward["accounts"]
