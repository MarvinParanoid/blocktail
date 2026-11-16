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

The previous release is a git ref, BLOCKTAIL_ROLLBACK_BASE (CI: origin/main,
which production runs until this is merged). Without it the test is skipped;
the CI job that sets it sets BLOCKTAIL_ROLLBACK_REQUIRED=1, so there it fails
instead of skipping when the ref is missing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PHASES = ROOT / "tests" / "rollback_phases.py"
BASE = os.environ.get("BLOCKTAIL_ROLLBACK_BASE", "")
REQUIRED = os.environ.get("BLOCKTAIL_ROLLBACK_REQUIRED") == "1"


def _previous_release(tmp_path: Path) -> Path:
    if not BASE:
        if REQUIRED:
            pytest.fail("BLOCKTAIL_ROLLBACK_REQUIRED=1 but BLOCKTAIL_ROLLBACK_BASE is not set")
        pytest.skip("set BLOCKTAIL_ROLLBACK_BASE to the git ref production runs (origin/main)")
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BASE],
                             capture_output=True, check=False)
    if archive.returncode != 0:
        message = f"cannot read {BASE}: {archive.stderr.decode().strip()}"
        if REQUIRED:
            pytest.fail(message)
        pytest.skip(message)
    target = tmp_path / "previous"
    target.mkdir()
    with tarfile.open(fileobj=BytesIO(archive.stdout)) as tar:
        tar.extractall(target, filter="data")
    return target


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
    previous = _previous_release(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    ours = sorted(int(p.name[:3]) for p in (ROOT / "app" / "migrations").glob("[0-9]*.sql"))

    before = _phase(previous, "old1", work)
    migrated = _phase(ROOT, "new", work)
    assert migrated["schema"]["versions"] == ours, migrated["schema"]["versions"]
    # A release without a migration is fine; then this shows only that the
    # previous release runs on its own schema after this one has used it.
    assert set(before["schema"]["versions"]) <= set(ours)

    back = _phase(previous, "old2", work)
    # Reads answer as before. The one expected difference is the history floor
    # ("indexed back to block #…"): this release went further back when asked,
    # and the previous one shows where the index now reaches.
    snap6 = json.loads((work / "snap6.json").read_text())
    snap8 = json.loads((work / "snap8.json").read_text())
    assert back["reads_compared"] >= 20
    for path in back["reads_differing"]:
        assert snap6[path][0] == snap8[path][0], f"{path}: status changed"
        old = [line for line in snap6[path][1].splitlines() if line not in snap8[path][1]]
        new = [line for line in snap8[path][1].splitlines() if line not in snap6[path][1]]
        unexpected = [line for line in old + new if "indexed back to block" not in line]
        assert not unexpected, f"{path} differs after the migration: {unexpected[:5]}"
    # Writes on the migrated file: a wallet added and renamed (its transfer
    # indexed), another removed.
    assert back["later_rows"] >= 1
    assert "Later renamed" in back["accounts"] and "Trading desk" not in back["accounts"]
    assert back["schema"]["versions"] == ours

    forward = _phase(ROOT, "new2", work)
    assert "Later renamed" in forward["accounts"] and "Trading desk" not in forward["accounts"]
