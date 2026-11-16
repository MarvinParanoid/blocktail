"""The container, compose and CI configuration (S8b).

Two kinds of check. Static ones read Dockerfile, docker-compose.yml,
.dockerignore and the workflows and hold them to what docs/production.md
promises. Process ones run the image's own command — the CMD from the
Dockerfile, through `sh -c` exactly as the container does — on this machine,
against a provider address that refuses connections: no Docker, no network.

What only a real Docker daemon can show (non-root at runtime, the read-only
root filesystem, capabilities, the named volume's ownership, `docker stop`)
is in scripts/container-smoke.sh, which CI runs on every push.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICE = COMPOSE["services"]["blocktail"]
WORKFLOWS = {p.name: yaml.safe_load(p.read_text())
             for p in (ROOT / ".github" / "workflows").glob("*.yml")}
SECRET_NAMES = ("ALCHEMY_API_KEY", "ALCHEMY_URL", "AUTH_PASSWORD", "AUTH_USER")


def instructions(dockerfile: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments), continuation lines joined, comments dropped."""
    text = re.sub(r"\\\n", " ", dockerfile)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            out.append((word.upper(), rest.strip()))
    return out


DOCKER = instructions(DOCKERFILE)


def cmd() -> list[str]:
    return json.loads(next(rest for word, rest in DOCKER if word == "CMD"))


# ----------------------------------------------------------------- Dockerfile


def test_the_app_runs_as_its_own_unprivileged_user():
    users = [rest for word, rest in DOCKER if word == "USER"]
    assert users and users[-1] not in ("root", "0", "0:0")
    last_user = max(i for i, (word, _) in enumerate(DOCKER) if word == "USER")
    assert all(word != "RUN" for word, _ in DOCKER[last_user:]), \
        "nothing runs as root after the switch"


def test_the_user_is_created_the_way_existing_volumes_expect():
    """The data volume on a running host is owned by the uid this line gave
    the user. Pinning a different uid would leave the next image unable to
    write its own database, and the backup step (which runs as the image's
    user) unable to read it."""
    assert "adduser --system --group --no-create-home blocktail" in DOCKERFILE
    assert "chown -R blocktail:blocktail /data" in DOCKERFILE


def test_only_the_code_and_its_requirements_go_into_the_image():
    copied = [rest.split()[0] for word, rest in DOCKER if word in ("COPY", "ADD")]
    assert copied == ["requirements.txt", "app"]
    assert not any(word == "ADD" for word, _ in DOCKER)


def test_no_secret_is_set_in_the_image():
    for word, rest in DOCKER:
        if word in ("ENV", "ARG"):
            for name in SECRET_NAMES:
                assert name not in rest, (word, rest)


def test_the_build_context_leaves_local_state_out():
    ignored = (ROOT / ".dockerignore").read_text().split()
    for entry in (".env", "wallets.yml", "data/", "*.db", "*.db-wal", "*.db-shm", ".git/"):
        assert entry in ignored, entry


def test_the_database_lives_in_the_data_volume():
    env = " ".join(rest for word, rest in DOCKER if word == "ENV")
    assert "BLOCKTAIL_DB=/data/blocktail.db" in env
    assert "PYTHONDONTWRITEBYTECODE=1" in env, "nothing written beside the code at runtime"


def test_the_server_command_is_one_worker_with_no_proxy_trust_and_a_bounded_stop():
    command = cmd()
    assert command[:2] == ["sh", "-c"]
    line = command[2]
    assert line.startswith("exec uvicorn "), "exec: uvicorn is PID 1 and gets SIGTERM itself"
    for flag in ("--workers 1", "--no-proxy-headers", "--no-server-header",
                 "--timeout-graceful-shutdown"):
        assert flag in line, flag


def test_the_healthcheck_asks_healthz_on_loopback():
    check = next(rest for word, rest in DOCKER if word == "HEALTHCHECK")
    assert "http://127.0.0.1:8000/healthz" in check
    assert SERVICE["healthcheck"]["test"][-1].count("http://127.0.0.1:8000/healthz") == 1


def test_one_stage_from_the_python_slim_image_pinned_by_digest():
    """A literal FROM (an ARG in FROM is invisible to Dependabot), the tag and
    its digest: the same build next month, and a tag Dependabot can follow —
    it proposes the new digest of the same tag as a pull request."""
    froms = [rest for word, rest in DOCKER if word == "FROM"]
    assert len(froms) == 1
    assert re.fullmatch(r"python:3\.13-slim@sha256:[0-9a-f]{64}", froms[0]), froms[0]


# -------------------------------------------------------------------- compose


def test_only_loopback_is_published():
    for port in SERVICE.get("ports", []):
        assert str(port).startswith("127.0.0.1:"), port
    assert "network_mode" not in SERVICE
    assert not SERVICE.get("privileged")
    assert "expose" not in SERVICE


def test_the_container_runs_with_least_privilege():
    assert SERVICE["read_only"] is True
    assert SERVICE["cap_drop"] == ["ALL"]
    assert "cap_add" not in SERVICE
    assert "no-new-privileges:true" in SERVICE["security_opt"]
    assert "user" not in SERVICE, "the image's user, not root"
    tmp = [t for t in SERVICE["tmpfs"] if t.startswith("/tmp:")]
    assert tmp and "size=" in tmp[0], "a bounded /tmp: SQLite's temp files land there"


def test_the_container_is_bounded():
    assert SERVICE["mem_limit"] == "512m"     # peak ~160 MB on 200k events
    assert int(SERVICE["pids_limit"]) >= 64
    assert SERVICE["restart"] == "unless-stopped"
    logging = SERVICE["logging"]
    assert logging["driver"] == "json-file"
    assert logging["options"]["max-size"] and logging["options"]["max-file"]


def test_compose_waits_longer_than_the_server_does_on_stop():
    graceful = int(re.search(r"--timeout-graceful-shutdown (\d+)", cmd()[2]).group(1))
    grace = SERVICE["stop_grace_period"]
    assert grace.endswith("s") and int(grace[:-1]) >= graceful + 5


def test_writable_paths_are_the_volume_and_tmp_only():
    volumes = SERVICE["volumes"]
    assert "blocktail-data:/data" in volumes
    assert "./wallets.yml:/config/wallets.yml:ro" in volumes
    writable = [v for v in volumes if not v.endswith(":ro")]
    assert writable == ["blocktail-data:/data"]
    assert "blocktail-data" in COMPOSE["volumes"]


def test_production_needs_an_explicit_host_list():
    """compose refuses to start without ALLOWED_HOSTS in .env — and the repo
    names no domain: the value is the operator's."""
    raw = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"ALLOWED_HOSTS: \$\{ALLOWED_HOSTS:\?[^}]+\}", raw)
    assert SERVICE["env_file"] == ".env"
    for name in SECRET_NAMES:
        assert name not in json.dumps(SERVICE.get("environment", {}))


# ------------------------------------------------------------------------ CI


def uses(workflow) -> list[str]:
    return [step["uses"] for job in workflow["jobs"].values()
            for step in job.get("steps", []) if "uses" in step]


def test_ci_reads_the_repository_and_nothing_more():
    for name, workflow in WORKFLOWS.items():
        assert workflow["permissions"] == {"contents": "read"}, name
        for job_name, job in workflow["jobs"].items():
            assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, job_name


def test_every_ci_job_has_a_timeout():
    for workflow in WORKFLOWS.values():
        for job_name, job in workflow["jobs"].items():
            assert 0 < int(job["timeout-minutes"]) <= 30, job_name


def test_every_action_is_pinned_to_a_commit():
    for workflow in WORKFLOWS.values():
        for ref in uses(workflow):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_ci_needs_no_secret_and_never_deploys():
    for name in WORKFLOWS:
        raw = (ROOT / ".github" / "workflows" / name).read_text()
        assert not re.search(r"\$\{\{\s*secrets\.", raw), name
        commands = " ".join(step.get("run", "") for job in WORKFLOWS[name]["jobs"].values()
                            for step in job["steps"])
        for word in ("deploy.sh", "remote-deploy", "ssh ", "BLOCKTAIL_HOST", "docker push",
                     "docker login"):
            assert word not in commands, (name, word)
        triggers = WORKFLOWS[name].get(True) or WORKFLOWS[name].get("on")   # yaml 1.1: on: -> True
        assert set(triggers) <= {"push", "pull_request", "workflow_dispatch"}, triggers
        assert set(triggers["push"]["branches"]) <= {"main", "master", "rc/**"}, \
            "pushes run checks on these branches only"


def test_ci_runs_the_checks_this_project_relies_on():
    wf = WORKFLOWS["tests.yml"]
    jobs = wf["jobs"]
    runs = {name: " ".join(s.get("run", "") for s in job["steps"]) for name, job in jobs.items()}
    assert jobs["pytest"]["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13"]
    assert "python -m pytest" in runs["pytest"]
    ruff = re.search(r"ruff==([\d.]+)", runs["lint"]).group(1)
    assert f"ruff=={ruff}" in (ROOT / "requirements-dev.txt").read_text(), "one ruff version"
    assert "playwright install --with-deps --only-shell chromium" in runs["ui-smoke"]
    assert "pytest tests_ui" in runs["ui-smoke"]
    artifact = [s for s in jobs["ui-smoke"]["steps"] if "upload-artifact" in s.get("uses", "")]
    assert artifact and artifact[0]["if"] == "failure()"
    assert "scripts/container-smoke.sh" in runs["image"]
    assert "pip-audit" in runs["audit"]


def test_dependencies_are_watched():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"] for u in config["updates"]}
    assert ecosystems == {"pip", "github-actions", "docker"}


def test_the_container_smoke_script_is_valid_bash():
    script = ROOT / "scripts" / "container-smoke.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    assert "set -euo pipefail" in script.read_text()


# ------------------------------------------------- the image's command, for real


KEY = "sk-container-test-0123456789"
PASSWORD = "container-test-password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, headers: dict | None = None) -> tuple[int, dict, str]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode()


@pytest.fixture
def server(tmp_path):
    """The image's CMD, with the image's environment, on this machine. The
    working directory is read-only, as the image's root filesystem is; the
    database directory is the only writable place besides TMPDIR."""
    code = tmp_path / "srv"
    code.mkdir()
    (code / "app").symlink_to(ROOT / "app")
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    (tmp_path / "wallets.yml").write_text(
        "wallets:\n  - name: Main\n    address: '0x" + "1" * 40 + "'\n")
    code.chmod(0o555)
    port = free_port()
    env = {
        "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "BLOCKTAIL_CONFIG": str(tmp_path / "wallets.yml"),
        "BLOCKTAIL_DB": str(data / "blocktail.db"),
        "HOST": "127.0.0.1", "PORT": str(port), "TMPDIR": str(tmp),
        # A provider that refuses at once: nothing leaves this machine.
        "ALCHEMY_URL": f"http://127.0.0.1:9/v2/{KEY}", "ALCHEMY_API_KEY": KEY,
        "PRICES": "off", "SYNC_INTERVAL_SECONDS": "3600",
        "AUTH_USER": "watcher", "AUTH_PASSWORD": PASSWORD,
        "ALLOWED_HOSTS": "watch.example.test",
    }
    log = tmp_path / "server.log"
    with log.open("w") as out:
        process = subprocess.Popen(cmd(), cwd=code, env=env, stdout=out,
                                   stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if get(base + "/healthz")[0] == 200:
                break
        except OSError:
            pass
        if process.poll() is not None:
            pytest.fail("the server exited: " + log.read_text())
        time.sleep(0.1)
    try:
        yield process, base, data, log, env
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        code.chmod(0o755)


def test_the_healthcheck_passes_without_credentials(server):
    process, base, *_ = server
    command = SERVICE["healthcheck"]["test"]
    probe = [sys.executable if c == "python" else c for c in command[1:]]
    probe[-1] = probe[-1].replace("http://127.0.0.1:8000", base)
    assert subprocess.run(probe, timeout=10).returncode == 0


def test_private_pages_need_credentials_and_the_public_host(server):
    _, base, *_ = server
    host = {"Host": "watch.example.test"}
    token = base64.b64encode(f"watcher:{PASSWORD}".encode()).decode()
    auth = {"Authorization": f"Basic {token}"}
    for path in ("/", "/api/status", "/activity", "/static/app.css"):
        status, headers, _ = get(base + path, host)
        assert status == 401, path
        assert headers["content-security-policy"].startswith("default-src 'none'")
    status, headers, _ = get(base + "/", {**host, **auth})
    assert status == 200
    assert "server" not in headers, "--no-server-header"
    # Loopback is the healthcheck's way in, nobody else's.
    assert get(base + "/", {"Host": "127.0.0.1", **auth})[0] == 400
    assert get(base + "/", {"Host": "localhost", **auth})[0] == 400


def test_sqlite_runs_in_wal_in_the_data_directory_only(server):
    _, base, data, *_ = server
    names = {p.name for p in data.iterdir()}
    assert "blocktail.db" in names and "blocktail.db-wal" in names, names
    conn = sqlite3.connect(data / "blocktail.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_the_server_stops_cleanly_on_sigterm_and_logs_no_secret(server):
    process, base, data, log, _ = server
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)       # what `docker stop` sends PID 1
    # uvicorn shuts down, then re-raises the signal it caught: the exit status
    # says "stopped by SIGTERM" (143 in docker ps), not "crashed".
    assert process.wait(timeout=25) in (0, -signal.SIGTERM)
    assert time.monotonic() - started < 20
    text = log.read_text()
    assert "Application shutdown complete" in text
    assert "Finished server process" in text
    token = base64.b64encode(f"watcher:{PASSWORD}".encode()).decode()
    for secret in (KEY, PASSWORD, token):
        assert secret not in text
    # Shut down, checkpointed: the database opens cleanly afterwards.
    conn = sqlite3.connect(data / "blocktail.db")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_the_command_line_is_what_the_container_runs():
    """The fixture runs cmd() unchanged; this keeps it honest about quoting."""
    line = cmd()[2]
    assert shlex.split(line)[0] == "exec"
