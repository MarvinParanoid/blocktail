"""scripts/rollback-base.sh: which release a rollback of HEAD returns to.

Each case builds a small repository with the history it describes and checks
the SHA the script prints — not merely that it ran. The rule: the newest
release tag (v<digits>…, not a pre-release) that HEAD contains, or
BLOCKTAIL_ROLLBACK_BASE when set; never HEAD^1 or the commit before a push.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "rollback-base.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="needs bash and git")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


class Repo:
    def __init__(self, path: Path):
        self.path = path
        (path / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, path / "scripts" / "rollback-base.sh")
        self.git("init", "-q", "-b", "main")
        self.n = 0

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True,
                              text=True, env={**os.environ, **GIT_ENV}).stdout.strip()

    def commit(self, count: int = 1, file: str = "f") -> str:
        for _ in range(count):
            self.n += 1
            (self.path / file).write_text(str(self.n))
            self.git("add", "-A")
            self.git("commit", "-q", "-m", f"c{self.n}")
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, annotated: bool = True) -> str:
        if annotated:
            self.git("tag", "-a", name, "-m", name)
        else:
            self.git("tag", name)
        return self.head()

    def base(self, **env) -> subprocess.CompletedProcess:
        clean = {k: v for k, v in os.environ.items() if k != "BLOCKTAIL_ROLLBACK_BASE"}
        return subprocess.run(["bash", str(self.path / "scripts" / "rollback-base.sh")],
                              cwd=self.path, capture_output=True, text=True, check=False,
                              env={**clean, **env})


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path / "repo")


def printed(result) -> str:
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_a_fast_forward_of_dozens_of_commits_still_returns_to_the_release(repo):
    repo.commit()
    release = repo.tag("v0.2.0")
    repo.commit(40)                         # one push, forty commits
    assert printed(repo.base()) == release
    assert repo.git("rev-parse", "HEAD^1") != release, "HEAD^1 would have been wrong"


def test_one_commit_over_the_release(repo):
    repo.commit()
    release = repo.tag("v0.2.0")
    repo.commit()
    assert printed(repo.base()) == release


def test_several_commits_pushed_together_are_measured_against_the_release_not_the_push(repo):
    repo.commit()
    release = repo.tag("v0.2.0")
    before_push = repo.commit(2)            # what github.event.before would be
    repo.commit(3)
    base = printed(repo.base())
    assert base == release and base != before_push


def test_a_pull_request_merge_commit_with_intermediate_commits(repo):
    repo.commit()
    release = repo.tag("v0.2.0")
    repo.git("switch", "-q", "-c", "feature")
    repo.commit(4, file="feature")
    repo.git("switch", "-q", "main")
    repo.commit()                           # main moved on meanwhile
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "pull request")
    assert printed(repo.base()) == release


def test_a_release_branch(repo):
    repo.commit()
    release = repo.tag("v0.2.0")
    repo.git("switch", "-q", "-c", "rc/third-release")
    repo.commit(5)
    assert printed(repo.base()) == release


def test_the_newest_release_the_history_contains_wins(repo):
    repo.commit()
    repo.tag("v0.1.0")
    repo.commit(3)
    newer = repo.tag("v0.2.0")
    repo.commit(2)
    assert printed(repo.base()) == newer


def test_tags_that_are_not_releases_or_not_in_the_history_are_ignored(repo):
    repo.commit()
    release = repo.tag("v0.2.0")
    repo.commit()
    repo.tag("v0.3.0-rc1")                  # a pre-release
    repo.tag("deployed-somewhere", annotated=False)
    repo.git("switch", "-q", "-c", "elsewhere")
    repo.commit()
    repo.tag("v9.9.9")                      # newer, but not in main's history
    repo.git("switch", "-q", "main")
    repo.commit()
    assert printed(repo.base()) == release


def test_no_release_tag_fails_and_says_why(repo):
    repo.commit(3)
    result = repo.base()
    assert result.returncode != 0 and result.stdout == ""
    assert "no release tag" in result.stderr and "fetch-depth: 0" in result.stderr


def test_an_explicit_base_wins_and_must_exist(repo):
    first = repo.commit()
    repo.commit()
    repo.tag("v0.2.0")
    repo.commit()
    assert printed(repo.base(BLOCKTAIL_ROLLBACK_BASE=first[:7])) == first
    missing = repo.base(BLOCKTAIL_ROLLBACK_BASE="no-such-ref")
    assert missing.returncode != 0 and "not a commit" in missing.stderr


def test_head_that_is_the_release_is_its_own_base(repo):
    repo.commit(2)
    release = repo.tag("v0.2.0")
    assert printed(repo.base()) == release == repo.head()


def test_this_repository_returns_to_v0_2_0():
    """main now: v0.2.0 is c8193e8, the commit production runs."""
    if subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", "v0.2.0^{commit}"],
                      capture_output=True, check=False).returncode != 0:
        pytest.skip("the v0.2.0 tag is not fetched here")
    clean = {k: v for k, v in os.environ.items() if k != "BLOCKTAIL_ROLLBACK_BASE"}
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                            check=False, env=clean)
    assert printed(result) == "c8193e82118da2eb3608c62784e61e8d2bc5552a"
