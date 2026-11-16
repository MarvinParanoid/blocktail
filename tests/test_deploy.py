"""The deploy path, tested without a server, without Docker and without secrets.

Three layers, kept apart because they prove different things:

1. ``scripts/sqlite_backup.py`` against a real SQLite database in WAL mode,
   with a writer holding uncheckpointed changes — the case ``cp`` gets wrong.
2. ``scripts/deploy.sh --check-only`` in a throwaway git repository: what it
   refuses, and what the archive it would ship contains.
3. The whole ``deploy.sh`` → ``remote-deploy.sh`` flow with ``ssh`` and
   ``docker`` replaced by the doubles below. This checks the scripts' own
   logic — order of steps, what a failure stops, what gets printed, what a
   rollback pins — and **not** Docker: building, running and health-checking
   a real image is not exercised here.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
BACKUP_TOOL = SCRIPTS / "sqlite_backup.py"


def has_ruff() -> bool:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], capture_output=True, check=False
    ).returncode == 0


needs_ruff = pytest.mark.skipif(not has_ruff(), reason="ruff is not installed")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="needs bash and git")


# ------------------------------------------------------------- 1. backups


def make_wal_db(path: Path, rows: int = 50) -> sqlite3.Connection:
    """A blocktail-shaped database in WAL mode, with the last writes still in
    the -wal file. The returned connection keeps it that way."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")  # nothing reaches the main file
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER)")
    conn.execute("INSERT INTO schema_migrations VALUES (7, 0)")
    conn.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY, event_key TEXT)")
    conn.executemany("INSERT INTO activities (event_key) VALUES (?)",
                     [(f"k{i}",) for i in range(rows)])
    return conn


def tool(*args: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BACKUP_TOOL), *map(str, args)],
                          capture_output=True, text=True, check=False)


def test_a_backup_of_a_live_wal_database_has_every_row(tmp_path):
    db = tmp_path / "data" / "blocktail.db"
    db.parent.mkdir()
    writer = make_wal_db(db, rows=50)
    assert Path(f"{db}-wal").stat().st_size > 0, "precondition: rows only in the WAL"

    # What `cp` would have taken: the main file alone, without the WAL.
    naive = tmp_path / "naive.db"
    shutil.copyfile(db, naive)
    with pytest.raises(sqlite3.Error):
        sqlite3.connect(naive).execute("SELECT COUNT(*) FROM activities").fetchone()

    result = tool("backup", "--source", db, "--dest-dir", tmp_path / "backups", "--label", "t")
    writer.close()
    assert result.returncode == 0, result.stderr
    made = tmp_path / "backups" / result.stdout.strip()
    assert made.is_file()
    check = sqlite3.connect(made)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert check.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 50
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete", "one self-contained file"
    check.close()
    assert not list((tmp_path / "backups").glob(".partial-*"))


def test_a_backup_restores_and_the_app_can_open_it(tmp_path):
    db = tmp_path / "data" / "blocktail.db"
    db.parent.mkdir()
    make_wal_db(db, rows=12).close()
    name = tool("backup", "--source", db, "--dest-dir", tmp_path / "b").stdout.strip()
    made = tmp_path / "b" / name

    # Lose the database, then restore it — with the "app" stopped: the last
    # connection to close checkpoints the WAL away.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM activities")
    conn.commit()
    conn.close()
    result = tool("restore", "--backup", made, "--target", db)
    assert result.returncode == 0, result.stderr
    assert "previous file is kept" in result.stderr

    sys.path.insert(0, str(REPO))
    from app.db import Database

    restored = Database(db)
    conn = restored.connect()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 12
    restored.close()
    assert list(db.parent.glob("blocktail.db.before-restore-*")), "the replaced file is kept"
    assert tool("verify", "--path", made).returncode == 0


def test_restore_refuses_while_the_database_has_unwritten_changes(tmp_path):
    db = tmp_path / "blocktail.db"
    backup_db = tmp_path / "b"
    make_wal_db(db).close()
    made = backup_db / tool("backup", "--source", db, "--dest-dir", backup_db).stdout.strip()
    writer = make_wal_db(tmp_path / "live.db")  # a "running app"
    result = tool("restore", "--backup", made, "--target", tmp_path / "live.db")
    writer.close()
    assert result.returncode == 1
    assert "may still be running" in result.stderr


def test_a_failed_backup_leaves_nothing_behind_and_keeps_the_old_ones(tmp_path):
    db = tmp_path / "blocktail.db"
    make_wal_db(db).close()
    dest = tmp_path / "backups"
    first = tool("backup", "--source", db, "--dest-dir", dest)
    assert first.returncode == 0

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + os.urandom(8192))
    result = tool("backup", "--source", corrupt, "--dest-dir", dest)
    assert result.returncode == 1
    assert "failed" in result.stderr
    names = sorted(p.name for p in dest.iterdir())
    assert names == [first.stdout.strip()], "no partial file, and the good one is still there"


def test_only_the_last_five_backups_are_kept(tmp_path):
    db = tmp_path / "blocktail.db"
    make_wal_db(db).close()
    dest = tmp_path / "backups"
    for i in range(7):
        # Names carry a timestamp to the second; give each its own.
        (dest / f"blocktail-2026010{i}T000000Z-old.db").parent.mkdir(exist_ok=True)
        shutil.copyfile(db, dest / f"blocktail-2026010{i}T000000Z-old.db")
    result = tool("backup", "--source", db, "--dest-dir", dest, "--keep", "5")
    assert result.returncode == 0
    kept = sorted(p.name for p in dest.glob("blocktail-*.db"))
    assert len(kept) == 5
    assert result.stdout.strip() in kept, "the new one is never the one pruned"


def test_no_database_yet_is_not_an_error_when_asked(tmp_path):
    result = tool("backup", "--source", tmp_path / "absent.db", "--dest-dir", tmp_path,
                  "--missing-ok")
    assert result.returncode == 0 and result.stdout.strip() == "none"
    strict = tool("backup", "--source", tmp_path / "absent.db", "--dest-dir", tmp_path)
    assert strict.returncode == 1


# --------------------------------------------- 2. deploy.sh --check-only


GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


def make_project(tmp_path: Path) -> Path:
    """The smallest repository deploy.sh accepts as blocktail."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("deploy.sh", "remote-deploy.sh", "sqlite_backup.py"):
        shutil.copy2(SCRIPTS / name, repo / "scripts" / name)
    shutil.copy2(REPO / "pyproject.toml", repo / "pyproject.toml")
    shutil.copy2(REPO / ".gitignore", repo / ".gitignore")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    (repo / "docker-compose.yml").write_text("services: {}\n")
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def deploy(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "deploy.sh"), *args],
        cwd=repo, capture_output=True, text=True, check=False,
        env={**os.environ, **GIT_ENV, "BLOCKTAIL_PYTHON": sys.executable,
             "BLOCKTAIL_SSH": "/nonexistent/ssh-must-not-run", **(env or {})},
    )


@needs_bash
@needs_ruff
def test_check_only_ships_exactly_the_commit(tmp_path):
    repo = make_project(tmp_path)
    # Ignored local state: present on disk, invisible to `git status`.
    (repo / ".env").write_text("ALCHEMY_API_KEY=real-secret\n")
    (repo / "wallets.yml").write_text("wallets: []\n")
    (repo / "data").mkdir()
    (repo / "data" / "blocktail.db").write_bytes(b"x")
    (repo / "app" / "__pycache__").mkdir()
    (repo / "app" / "__pycache__" / "x.pyc").write_bytes(b"x")
    assert git(repo, "status", "--porcelain") == ""

    result = deploy(repo, "--check-only")
    assert result.returncode == 0, result.stderr
    sha = git(repo, "rev-parse", "--short=12", "HEAD").strip()
    assert f"commit {sha}" in result.stderr
    shipped = set(result.stdout.split())
    assert "scripts/remote-deploy.sh" in shipped and "app/__init__.py" in shipped
    for never in (".env", "wallets.yml", "data/blocktail.db", "app/__pycache__/x.pyc"):
        assert never not in shipped
    assert shipped <= set(git(repo, "ls-files").split()) | {"app/", "scripts/", "tests/"}


@needs_bash
@pytest.mark.parametrize("change", ["untracked", "modified", "staged"])
def test_a_dirty_tree_is_refused(tmp_path, change):
    repo = make_project(tmp_path)
    if change == "untracked":
        (repo / "app" / "new.py").write_text("x = 1\n")
    elif change == "modified":
        (repo / "app" / "__init__.py").write_text("y = 2\n")
    else:
        (repo / "app" / "__init__.py").write_text("y = 2\n")
        git(repo, "add", "app/__init__.py")
    result = deploy(repo, "--check-only")
    assert result.returncode == 1
    assert "working tree is not clean" in result.stderr


@needs_bash
def test_no_commit_is_refused(tmp_path):
    repo = tmp_path / "empty"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "deploy.sh", repo / "scripts" / "deploy.sh")
    shutil.copy2(REPO / "pyproject.toml", repo / "pyproject.toml")
    git(repo, "init", "-q")
    result = deploy(repo, "--check-only")
    assert result.returncode == 1
    assert "HEAD is not a commit" in result.stderr


@needs_bash
@needs_ruff
def test_failing_tests_are_refused(tmp_path):
    repo = make_project(tmp_path)
    (repo / "tests" / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "broken")
    result = deploy(repo, "--check-only")
    assert result.returncode == 1
    assert "tests failed" in result.stderr


@needs_bash
@needs_ruff
def test_lint_errors_are_refused(tmp_path):
    repo = make_project(tmp_path)
    (repo / "app" / "bad.py").write_text("def f():\n    return undefined_name\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "lint")
    result = deploy(repo, "--check-only")
    assert result.returncode == 1
    assert "ruff failed" in result.stderr


@needs_bash
def test_a_committed_secret_is_refused(tmp_path):
    repo = make_project(tmp_path)
    (repo / "deploy").mkdir()
    (repo / "deploy" / ".env").write_text("ALCHEMY_API_KEY=oops\n")
    git(repo, "add", "-f", "deploy/.env")
    git(repo, "commit", "-q", "-m", "oops")
    result = deploy(repo, "--check-only")
    assert result.returncode == 1
    assert "must never ship" in result.stderr


# ----------------------------------- 3. the remote flow, with doubles


FAKE_SSH = """#!/usr/bin/env bash
# Stands in for ssh: drops the host, runs the command here, passes stdin on.
shift
exec bash -c "$1"
"""

# Stands in for the docker CLI. It keeps a small state file and does the one
# thing a test can check for real: the backup, by running sqlite_backup.py on
# the host with the container paths mapped to directories under the stand.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import fcntl, hashlib, json, os, subprocess, sys, time
STATE = os.environ["FAKE_DOCKER_STATE"]
DATA = os.environ["FAKE_DOCKER_DATA"]   # what the data volume holds
CALLER = os.environ.get("FAKE_CALLER", "-")

guard = open(STATE + ".lock", "w")
fcntl.flock(guard, fcntl.LOCK_EX)       # the double's own bookkeeping, nothing more

def load():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {"images": {}, "container": None, "volume": False, "calls": []}

def save(s):
    # Atomically: the test reads this file without the lock, and open("w")
    # empties it before the new state is written (a race seen on CI).
    with open(STATE + ".tmp", "w") as out:
        json.dump(s, out, indent=1)
    os.replace(STATE + ".tmp", STATE)

s = load(); args = sys.argv[1:]; s["calls"].append(CALLER + ": " + " ".join(args)); save(s)
c = s["container"]

def fmt(f):
    return {
        "{{.Image}}": c and c["image_id"],
        "{{.Config.Image}}": c and c["config_image"],
        "{{.State.Status}}": c and c["status"],
        '{{index .Config.Labels "com.docker.compose.project"}}': c and c["project"],
        "{{range .Config.Env}}{{println .}}{{end}}": c and "BLOCKTAIL_DB=/data/blocktail.db\n",
    }.get(f, c and f"{c['status']} (restarts: 0, image {c['config_image']})")

if args[0] == "inspect":
    if c is None:
        sys.exit(1)
    if "-f" in args:
        print(fmt(args[args.index("-f") + 1]))
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    tag = args[-1]
    if tag not in s["images"]:
        sys.exit(1)
    print(s["images"][tag]); sys.exit(0)
if args[:2] == ["volume", "inspect"]:
    sys.exit(0 if s["volume"] else 1)
if args[0] == "build":
    if os.environ.get("FAKE_BUILD") == "fail":
        sys.exit(1)
    fcntl.flock(guard, fcntl.LOCK_UN)       # a slow build must not stall the others
    time.sleep(float(os.environ.get("FAKE_BUILD_SLEEP", "0")))
    fcntl.flock(guard, fcntl.LOCK_EX)
    s = load()
    tag = args[args.index("-t") + 1]
    digest = hashlib.sha256((tag + str(len(s["calls"]))).encode()).hexdigest()
    s["images"][tag] = "sha256:" + digest[:16]
    save(s); sys.exit(0)
if args[0] == "tag":
    src, dst = args[1], args[2]
    s["images"][dst] = s["images"].get(src, src); save(s); sys.exit(0)
if args[0] == "run":
    if "id" in args:
        print(os.getuid() if args[-1] == "-u" else os.getgid()); sys.exit(0)
    mounts = {"/data": DATA}
    for i, a in enumerate(args):
        if a == "-v":
            host, ctr = args[i + 1].split(":")[:2]
            if ctr != "/data":
                mounts[ctr] = host
    rest = args[args.index("/ops/sqlite_backup.py"):]
    def host_path(p):
        for ctr, host in mounts.items():
            if p == ctr or p.startswith(ctr + "/"):
                return host + p[len(ctr):]
        return p
    fcntl.flock(guard, fcntl.LOCK_UN)
    sys.exit(subprocess.call([sys.executable] + [host_path(a) for a in rest]))
if args[0] == "compose":
    if "up" in args:
        override = open("docker-compose.override.yml").read()
        tag = override.split("image:")[1].split()[0]
        s["container"] = {"image_id": s["images"][tag], "config_image": tag,
                          "status": "running", "project": args[args.index("-p") + 1]}
        s["volume"] = True
        save(s)
    sys.exit(0)
if args[0] == "exec":
    if os.environ.get("FAKE_HEALTH") == "fail":
        sys.exit(1)
    print('{"status": "ok"}'); sys.exit(0)
if args[0] == "logs":
    print("fake app log: migration 008 failed"); sys.exit(0)
sys.exit(f"fake docker: unhandled {args}")
'''


# What the server's .env holds: never shipped, never touched by a deploy.
SERVER_ENV = "ALCHEMY_API_KEY=server-side-only\nALLOWED_HOSTS=watch.example.test\n"


class Stand:
    """A local stand-in for the VPS: a remote directory, a data volume, and
    doubles for ssh and docker on the command line."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.repo = make_project_with_real_scripts(tmp_path)
        self.remote = tmp_path / "server" / "blocktail-build"
        self.remote.mkdir(parents=True)
        (self.remote / ".env").write_text(SERVER_ENV)
        (self.remote / "wallets.yml").write_text("wallets: []\n")
        self.data = tmp_path / "volume"
        self.data.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH)
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        for name in ("ssh", "docker"):
            (bin_dir / name).chmod(0o755)
        self.state = tmp_path / "docker-state.json"
        self.env = {
            "BLOCKTAIL_SSH": str(bin_dir / "ssh"),
            "DOCKER": str(bin_dir / "docker"),
            "BLOCKTAIL_REMOTE": str(self.remote),
            "BLOCKTAIL_HOST": "test-host",
            "BLOCKTAIL_HEALTH_TIMEOUT": "1",
            "BLOCKTAIL_HEALTH_INTERVAL": "0.1",
            "FAKE_DOCKER_STATE": str(self.state),
            "FAKE_DOCKER_DATA": str(self.data),
        }

    def commit(self, message: str) -> str:
        (self.repo / "app" / "version.py").write_text(f"VERSION = {message!r}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "--short=12", "HEAD").strip()

    def deploy(self, **env) -> subprocess.CompletedProcess:
        return deploy(self.repo, env={**self.env, **env})

    def script(self) -> Path:
        return next((self.remote / "releases").glob("*/scripts/remote-deploy.sh"))

    def remote_script(self, *args, **env) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.script()), *args], capture_output=True,
                              text=True, check=False, env={**os.environ, **self.env, **env})

    def start_remote(self, *args, **env) -> subprocess.Popen:
        """The server script in the background, in its own process group."""
        return subprocess.Popen(["bash", str(self.script()), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True,
                                env={**os.environ, **self.env, **env})

    def incoming_copy(self, sha: str) -> Path:
        """What a deploy session unpacks before it asks for the lock."""
        target = self.remote / "releases" / f".incoming-{sha}-test-{os.getpid()}"
        shutil.copytree(self.remote / "releases" / sha, target)
        return target

    @property
    def docker(self) -> dict:
        return json.loads(self.state.read_text())

    def calls_by(self, caller: str) -> list[str]:
        return [c for c in self.docker["calls"] if c.startswith(caller + ": ")]

    def wait_for_call(self, needle: str, timeout: float = 15) -> None:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state.exists() and any(needle in c for c in self.docker["calls"]):
                return
            time.sleep(0.05)
        raise AssertionError(f"never saw {needle!r}")

    def pinned(self) -> str:
        text = (self.remote / "docker-compose.override.yml").read_text()
        return text.split("image:")[1].split()[0]

    def last_good(self) -> tuple[str, str]:
        tag, image = (self.remote / "last-good").read_text().split()
        return tag, image

    def backups(self) -> list[Path]:
        return sorted((self.remote / "backups").glob("blocktail-*.db"))


def make_project_with_real_scripts(tmp_path: Path) -> Path:
    repo = make_project(tmp_path)
    shutil.copy2(REPO / "docker-compose.yml", repo / "docker-compose.yml")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "compose")
    return repo


@pytest.fixture
def stand(tmp_path):
    if not has_ruff() or shutil.which("bash") is None:
        pytest.skip("needs bash, git and ruff")
    return Stand(tmp_path)


def deployed(stand: Stand, message: str, **env) -> str:
    sha = stand.commit(message)
    result = stand.deploy(**env)
    assert result.returncode == 0, result.stderr
    return sha


# ---------------------------------------------------- a deploy, step by step


def test_a_first_deploy_has_nothing_to_back_up(stand):
    sha = stand.commit("v1")
    result = stand.deploy()
    assert result.returncode == 0, result.stderr
    assert "first deployment, nothing to back up" in result.stderr
    assert stand.pinned() == f"blocktail:{sha}"
    assert stand.docker["container"]["config_image"] == f"blocktail:{sha}"
    assert stand.last_good()[0] == f"blocktail:{sha}"
    assert "healthz is liveness only" in result.stderr
    assert (stand.remote / ".env").read_text() == SERVER_ENV
    release = stand.remote / "releases" / sha
    assert not (release / ".env").exists() and (release / "scripts" / "sqlite_backup.py").exists()
    assert not list((stand.remote / "releases").glob(".incoming-*")), "moved into place"
    assert f"{sha} ok" in (stand.remote / "deployments.log").read_text()


def test_a_deploy_backs_up_the_live_database_first(stand):
    v1 = deployed(stand, "v1")
    writer = make_wal_db(stand.data / "blocktail.db", rows=30)  # the "running" app

    v2 = stand.commit("v2")
    result = stand.deploy()
    writer.close()
    assert result.returncode == 0, result.stderr

    [made] = stand.backups()
    assert made.name.endswith(f"before-{v2}-from-{v1}.db")
    conn = sqlite3.connect(made)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 30
    conn.close()

    assert stand.pinned() == f"blocktail:{v2}"
    assert stand.last_good() == (f"blocktail:{v2}", stand.docker["images"][f"blocktail:{v2}"])
    calls = stand.docker["calls"]
    backup_at = next(i for i, c in enumerate(calls) if "sqlite_backup.py" in c)
    up_at = max(i for i, c in enumerate(calls) if " compose " in f" {c} " and " up " in c)
    assert backup_at < up_at, "the backup is taken before the new version starts"


def test_a_failed_backup_stops_the_deploy_before_anything_changes(stand):
    deployed(stand, "v1")
    make_wal_db(stand.data / "blocktail.db").close()
    v2 = deployed(stand, "v2")
    good = stand.backups()

    (stand.data / "blocktail.db").write_bytes(b"SQLite format 3\x00" + os.urandom(8192))
    stand.commit("v3")
    ups_before = sum(" up " in c for c in stand.docker["calls"])
    result = stand.deploy()
    assert result.returncode != 0
    assert "database backup failed; the running version is untouched" in result.stderr
    assert sum(" up " in c for c in stand.docker["calls"]) == ups_before, "nothing was started"
    assert stand.pinned() == f"blocktail:{v2}", "still pinned to the running version"
    assert stand.last_good()[0] == f"blocktail:{v2}"
    assert stand.backups() == good, "the earlier backups are still there"
    assert "aborted: backup failed" in (stand.remote / "deployments.log").read_text()


def test_a_build_failure_leaves_the_running_version(stand):
    v1 = deployed(stand, "v1")
    stand.commit("v2")
    result = stand.deploy(FAKE_BUILD="fail")
    assert result.returncode != 0
    assert "image build failed; the running version is untouched" in result.stderr
    assert stand.pinned() == f"blocktail:{v1}"
    assert stand.last_good()[0] == f"blocktail:{v1}"


def test_compose_runs_in_the_same_project_so_the_volume_is_the_same(stand):
    """The data volume is named after the compose project; a deploy must keep
    using the one the running container belongs to."""
    deployed(stand, "v1")
    state = stand.docker
    state["container"]["project"] = "legacy-name"
    stand.state.write_text(json.dumps(state))
    deployed(stand, "v2")
    assert any("compose -p legacy-name up" in c for c in stand.docker["calls"])


def test_five_backups_and_five_releases_are_kept(stand):
    deployed(stand, "v0")
    make_wal_db(stand.data / "blocktail.db").close()
    shas = [deployed(stand, f"v{i}") for i in range(1, 8)]
    assert len(stand.backups()) == 5
    releases = {p.name for p in (stand.remote / "releases").iterdir()}
    assert len(releases) == 5
    assert shas[-1] in releases, "the running release is never pruned"


# ------------------------------------------------ what counts as last good


def test_a_failed_health_check_points_at_the_last_good_sha(stand):
    v1 = deployed(stand, "v1")
    make_wal_db(stand.data / "blocktail.db").close()
    v2 = stand.commit("v2")
    result = stand.deploy(FAKE_HEALTH="fail")
    assert result.returncode != 0
    assert "did not become healthy" in result.stderr
    assert "fake app log: migration 008 failed" in result.stderr
    assert f"rollback blocktail:{v1}" in result.stderr, "an immutable tag, not 'previous'"
    assert "Nothing was rolled back automatically" in result.stderr
    [made] = stand.backups()
    assert str(made) in result.stderr and made.name.endswith(f"-from-{v1}.db")
    assert stand.last_good()[0] == f"blocktail:{v1}", "a failure never rewrites the record"
    assert f"{v2} failed health check" in (stand.remote / "deployments.log").read_text()


def test_repeated_failed_deploys_keep_the_way_back(stand):
    """Each retry backs up the failed version and would rotate older backups
    out; the last good version, its image and its newest backup survive."""
    v1 = deployed(stand, "v1")
    make_wal_db(stand.data / "blocktail.db").close()
    v1_image = stand.docker["images"][f"blocktail:{v1}"]
    v2 = stand.commit("v2")
    assert stand.deploy(FAKE_HEALTH="fail").returncode != 0
    for attempt in range(6):  # retries on the server; deploy.sh's own checks are tested above
        result = stand.remote_script("deploy", v2, stand.incoming_copy(v2), FAKE_HEALTH="fail")
        assert result.returncode != 0
        assert f"rollback blocktail:{v1}" in result.stderr, f"attempt {attempt}"
    builds = [c for c in stand.docker["calls"] if c.startswith("-: build")]
    assert len(builds) == 2, "v1 once, v2 once: a commit's image is never rebuilt"
    assert stand.last_good() == (f"blocktail:{v1}", v1_image)
    from_v1 = [p for p in stand.backups() if p.name.endswith(f"-from-{v1}.db")]
    assert len(from_v1) == 1, "the backup taken from the good version is still there"
    assert len(stand.backups()) <= 6

    result = stand.remote_script("rollback")
    assert result.returncode == 0, result.stderr
    assert stand.docker["container"]["image_id"] == v1_image
    assert stand.pinned() == f"blocktail:{v1}"


def test_rollback_defaults_to_the_last_good_version_and_leaves_the_database(stand):
    v1 = deployed(stand, "v1")
    make_wal_db(stand.data / "blocktail.db").close()
    stand.commit("v2")
    assert stand.deploy(FAKE_HEALTH="fail").returncode != 0
    db_before = (stand.data / "blocktail.db").read_bytes()

    result = stand.remote_script("rollback")
    assert result.returncode == 0, result.stderr
    assert "the database is NOT restored" in result.stderr
    assert stand.pinned() == f"blocktail:{v1}"
    assert (stand.data / "blocktail.db").read_bytes() == db_before


def test_rollback_to_a_named_sha_records_it_as_good(stand):
    v1 = deployed(stand, "v1")
    v2 = deployed(stand, "v2")
    result = stand.remote_script("rollback", f"blocktail:{v1}")
    assert result.returncode == 0, result.stderr
    assert stand.last_good()[0] == f"blocktail:{v1}"
    assert v2  # still on the host for a roll forward
    assert f"blocktail:{v2}" in stand.docker["images"]


def test_rollback_refuses_a_good_tag_that_was_rebuilt_by_hand(stand):
    v1 = deployed(stand, "v1")
    deployed(stand, "v2")
    stand.remote_script("rollback", f"blocktail:{v1}")
    state = stand.docker
    state["images"][f"blocktail:{v1}"] = "sha256:somethingelse"
    stand.state.write_text(json.dumps(state))
    result = stand.remote_script("rollback")
    assert result.returncode != 0
    assert "rebuilt or retagged by hand" in result.stderr


def test_rollback_to_an_unknown_image_is_refused(stand):
    deployed(stand, "v1")
    result = stand.remote_script("rollback", "blocktail:nope")
    assert result.returncode != 0
    assert "no image blocktail:nope" in result.stderr


def test_the_first_scripted_deploy_adopts_what_is_running_as_good(stand):
    """A server deployed by the old script: a container runs blocktail:0.1.0,
    and nothing is recorded. That image becomes the baseline to return to."""
    stand.state.write_text(json.dumps({
        "images": {"blocktail:0.1.0": "sha256:legacy000000000"},
        "container": {"image_id": "sha256:legacy000000000", "config_image": "blocktail:0.1.0",
                      "status": "running", "project": "blocktail-build"},
        "volume": True, "calls": [],
    }))
    make_wal_db(stand.data / "blocktail.db").close()
    stand.commit("v1")
    result = stand.deploy(FAKE_HEALTH="fail")
    assert result.returncode != 0
    assert "recorded as blocktail:baseline-legacy000000" in result.stderr
    assert "rollback blocktail:baseline-legacy000000" in result.stderr
    [made] = stand.backups()
    assert made.name.endswith("-from-baseline-legacy000000.db")

    assert stand.remote_script("rollback").returncode == 0
    assert stand.docker["container"]["image_id"] == "sha256:legacy000000000"


@pytest.mark.parametrize("env", [
    "ALCHEMY_API_KEY=server-side-only\n",
    "ALCHEMY_API_KEY=server-side-only\nALLOWED_HOSTS=\n",
    "ALCHEMY_API_KEY=server-side-only\n# ALLOWED_HOSTS=watch.example.test\n",
])
def test_a_deploy_without_allowed_hosts_is_refused_before_anything_changes(stand, env):
    """S8b: production serves its own names only. compose would refuse too —
    this says why, before a build, a backup or a stop."""
    v1 = deployed(stand, "v1")
    compose_before = (stand.remote / "docker-compose.yml").read_text()
    (stand.remote / ".env").write_text(env)
    v2 = stand.commit("v2")
    result = stand.deploy()
    assert result.returncode != 0
    assert "ALLOWED_HOSTS" in result.stderr and "nothing was changed" in result.stderr
    assert f"blocktail:{v2}" not in stand.docker["images"], "not built"
    assert stand.pinned() == f"blocktail:{v1}"
    assert (stand.remote / "docker-compose.yml").read_text() == compose_before
    assert stand.backups() == []


def test_a_rollback_without_allowed_hosts_says_why_instead_of_failing_in_compose(stand):
    v1 = deployed(stand, "v1")
    deployed(stand, "v2")
    (stand.remote / ".env").write_text("ALCHEMY_API_KEY=server-side-only\n")
    result = stand.remote_script("rollback", f"blocktail:{v1}")
    assert result.returncode != 0
    assert "ALLOWED_HOSTS" in result.stderr
    assert stand.pinned() != f"blocktail:{v1}"


def test_rollback_with_nothing_recorded_asks_for_a_tag(stand):
    deployed(stand, "v1")
    (stand.remote / "last-good").unlink()
    result = stand.remote_script("rollback")
    assert result.returncode != 0
    assert "name an image tag" in result.stderr


# ----------------------------------------------------------------- the lock


@pytest.fixture(params=["flock", "lock directory"])
def lock_kind(request, stand):
    """Every lock test runs with flock and with the fallback used where flock
    is missing."""
    return {} if request.param == "flock" else {"BLOCKTAIL_LOCK_FALLBACK": "1"}


def test_a_second_deploy_is_refused_and_changes_nothing(stand, lock_kind):
    v1 = deployed(stand, "v1")
    v2 = stand.commit("v2")
    stand.deploy(FAKE_BUILD="fail")  # ships releases/<v2>, builds nothing
    first = stand.start_remote("deploy", v2, stand.incoming_copy(v2),
                               FAKE_BUILD_SLEEP="3", FAKE_CALLER="A", **lock_kind)
    try:
        stand.wait_for_call("A: build")  # A holds the lock and is mid-build
        pinned_before = stand.pinned()
        mine = stand.incoming_copy(v2)
        second = stand.remote_script("deploy", v2, mine, FAKE_CALLER="B", **lock_kind)
        assert second.returncode != 0
        assert "another deploy or rollback is running" in second.stderr
        assert f"deploy {v2}" in second.stderr, "says who holds it"
        assert stand.calls_by("B") == [], "refused before touching docker"
        assert not mine.exists(), "its own unpacked tree is removed"
        assert stand.pinned() == pinned_before == f"blocktail:{v1}"
        assert stand.last_good()[0] == f"blocktail:{v1}"
    finally:
        _, err = first.communicate(timeout=60)
    assert first.returncode == 0, err
    assert stand.last_good()[0] == f"blocktail:{v2}", "the first deploy was not disturbed"


def test_a_rollback_during_a_deploy_is_refused(stand, lock_kind):
    v1 = deployed(stand, "v1")
    v2 = stand.commit("v2")
    stand.deploy(FAKE_BUILD="fail")  # ships releases/<v2>, builds nothing
    first = stand.start_remote("deploy", v2, stand.incoming_copy(v2),
                               FAKE_BUILD_SLEEP="3", FAKE_CALLER="A", **lock_kind)
    try:
        stand.wait_for_call("A: build")
        rollback = stand.remote_script("rollback", FAKE_CALLER="B", **lock_kind)
        assert rollback.returncode != 0
        assert "another deploy or rollback is running" in rollback.stderr
        assert stand.calls_by("B") == []
        assert stand.last_good()[0] == f"blocktail:{v1}"
    finally:
        _, err = first.communicate(timeout=60)
    assert first.returncode == 0, err
    assert stand.last_good()[0] == f"blocktail:{v2}", "the deploy itself was not disturbed"


def test_a_crashed_deploy_does_not_hold_the_lock(stand, lock_kind):
    import signal

    deployed(stand, "v1")
    v2 = stand.commit("v2")
    stand.deploy(FAKE_BUILD="fail")
    first = stand.start_remote("deploy", v2, stand.incoming_copy(v2),
                               FAKE_BUILD_SLEEP="30", FAKE_CALLER="A", **lock_kind)
    stand.wait_for_call("A: build")
    os.killpg(first.pid, signal.SIGKILL)  # no trap runs, no cleanup
    first.communicate(timeout=10)

    result = stand.remote_script("deploy", v2, stand.incoming_copy(v2), **lock_kind)
    assert result.returncode == 0, result.stderr
    if lock_kind:
        assert "removing a stale lock" in result.stderr
    assert stand.last_good()[0] == f"blocktail:{v2}"


def test_deploy_sh_ships_into_a_private_directory(stand):
    """Two sessions never unpack into the same place; the server moves the
    tree into releases/<sha> only once it holds the lock."""
    sha = deployed(stand, "v1")
    assert (stand.remote / "releases" / sha / "Dockerfile").exists()
    assert not list((stand.remote / "releases").glob(".incoming-*"))


def test_a_long_label_is_not_cut_short(tmp_path):
    """The deploy finds "the backup taken from the last good version" by the
    end of its name; a label cut at a fixed length would hide it."""
    db = tmp_path / "blocktail.db"
    make_wal_db(db).close()
    label = "before-0123456789ab-from-baseline-0123456789ab"
    made = tool("backup", "--source", db, "--dest-dir", tmp_path / "b", "--label", label)
    assert made.returncode == 0
    assert made.stdout.strip().endswith(f"-{label}.db")


def test_the_protected_backup_survives_rotation(tmp_path):
    db = tmp_path / "blocktail.db"
    make_wal_db(db).close()
    dest = tmp_path / "backups"
    dest.mkdir()
    shutil.copyfile(db, dest / "blocktail-20260101T000000Z-before-v2-from-v1.db")
    for i in range(6):
        shutil.copyfile(db, dest / f"blocktail-2026010{i + 2}T000000Z-before-v2-from-v2.db")
    result = tool("backup", "--source", db, "--dest-dir", dest, "--keep", "5",
                  "--label", "before-v2-from-v2", "--protect=-from-v1.db")
    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in dest.glob("blocktail-*.db"))
    assert "blocktail-20260101T000000Z-before-v2-from-v1.db" in names
    assert len(names) == 6, "five newest, plus the protected one"
