"""The repository remote and pull requests.

Two backends with the same behavior:
- local: a bare git repository on disk plus a PR table, used for repeatable runs.
  A pre-receive hook rejects any push that does not come through the gateway.
- github: a real GitHub repository and its REST API.

Both enforce the two storage-level rules Hot Swap relies on:
- pushes use --force-with-lease, so a writer that expects an old branch head is refused;
- only one open pull request may exist per head and base branch.
"""
from __future__ import annotations

import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from hotswap.gitutil import GitResult, git, git_async
from hotswap.gateway.store import Store

HOOK = """#!/bin/sh
expected="$(cat "$GIT_DIR/hotswap-token" 2>/dev/null)"
if [ -z "$HOTSWAP_PUSH_TOKEN" ] || [ "$HOTSWAP_PUSH_TOKEN" != "$expected" ]; then
  echo "rejected: writes to this repository must go through the Hot Swap gateway" >&2
  exit 1
fi
exit 0
"""


class PullRequestExists(Exception):
    def __init__(self, number: int) -> None:
        super().__init__(f"a pull request already exists for this branch (#{number})")
        self.number = number


@dataclass
class PullRequest:
    number: int
    head: str
    base: str
    title: str
    body: str
    state: str
    url: str


class Remote:
    kind = "base"

    def read_url(self) -> str: ...
    def write_url(self) -> str: ...
    def push_env(self) -> dict: return {}

    async def push(self, source: Path, sha: str, branch: str, expected: str | None) -> GitResult:
        lease = f"--force-with-lease=refs/heads/{branch}:{expected or ''}"
        return await git_async(
            source, "push", "--porcelain", self.write_url(), f"{sha}:refs/heads/{branch}", lease,
            extra_env=self.push_env(),
        )

    def branch_head(self, branch: str) -> str | None:
        out = git(Path.cwd(), "ls-remote", self.read_url(), f"refs/heads/{branch}").out
        return out.split()[0] if out else None

    def find_open_pr(self, head: str, base: str) -> PullRequest | None: ...
    def create_pr(self, head: str, base: str, title: str, body: str, author: str) -> PullRequest: ...
    def update_pr_body(self, number: int, body: str) -> None: ...
    def list_prs(self) -> list[PullRequest]: ...


class LocalRemote(Remote):
    kind = "local"

    def __init__(self, path: Path, store: Store) -> None:
        self.path = path
        self.store = store

    @property
    def token(self) -> str:
        return (self.path / "hotswap-token").read_text().strip()

    def read_url(self) -> str:
        return self.path.as_posix()

    def write_url(self) -> str:
        return self.path.as_posix()

    def push_env(self) -> dict:
        return {"HOTSWAP_PUSH_TOKEN": self.token}

    def create(self, source_dir: Path, token: str) -> None:
        """Creates the bare repository from a project directory, with the push guard installed."""
        if self.path.exists():
            shutil.rmtree(self.path, onerror=_force_remove)
        self.path.mkdir(parents=True)
        git(self.path, "init", "-q", "--bare", "-b", "main", check=True)
        (self.path / "hotswap-token").write_text(token)
        hook = self.path / "hooks" / "pre-receive"
        hook.write_text(HOOK, newline="\n")
        hook.chmod(0o755)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "seed"
            shutil.copytree(source_dir, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            git(work, "init", "-q", "-b", "main", check=True)
            git(work, "add", "-A", check=True)
            git(work, "commit", "-q", "-m", "ledgerlite: initial version", check=True)
            pushed = git(work, "push", "-q", self.write_url(), "main", extra_env=self.push_env())
            if not pushed.ok:
                raise RuntimeError(pushed.err)

    def find_open_pr(self, head: str, base: str) -> PullRequest | None:
        row = self.store.one("SELECT * FROM prs WHERE head = ? AND base = ? AND state = 'open'", (head, base))
        return self._pr(row) if row else None

    def create_pr(self, head: str, base: str, title: str, body: str, author: str) -> PullRequest:
        existing = self.find_open_pr(head, base)
        if existing:
            raise PullRequestExists(existing.number)
        cursor = self.store.execute(
            "INSERT INTO prs (head, base, title, body, state, created_by, created_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
            (head, base, title, body, author, time.time()),
        )
        return self._pr(self.store.one("SELECT * FROM prs WHERE number = ?", (cursor.lastrowid,)))

    def update_pr_body(self, number: int, body: str) -> None:
        self.store.execute("UPDATE prs SET body = ? WHERE number = ?", (body, number))

    def list_prs(self) -> list[PullRequest]:
        return [self._pr(row) for row in self.store.all("SELECT * FROM prs ORDER BY number")]

    @staticmethod
    def _pr(row: dict) -> PullRequest:
        return PullRequest(row["number"], row["head"], row["base"], row["title"], row["body"], row["state"],
                           f"local://pull/{row['number']}")


class GitHubRemote(Remote):
    kind = "github"

    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token
        self.http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )

    def read_url(self) -> str:
        return f"https://github.com/{self.repo}.git"

    def write_url(self) -> str:
        return f"https://x-access-token:{self.token}@github.com/{self.repo}.git"

    def find_open_pr(self, head: str, base: str) -> PullRequest | None:
        owner = self.repo.split("/")[0]
        resp = self.http.get(f"/repos/{self.repo}/pulls", params={"head": f"{owner}:{head}", "base": base, "state": "open"})
        resp.raise_for_status()
        items = resp.json()
        return self._pr(items[0]) if items else None

    def create_pr(self, head: str, base: str, title: str, body: str, author: str) -> PullRequest:
        resp = self.http.post(f"/repos/{self.repo}/pulls", json={"head": head, "base": base, "title": title, "body": body, "draft": True})
        if resp.status_code == 422 and "already exists" in resp.text:
            existing = self.find_open_pr(head, base)
            raise PullRequestExists(existing.number if existing else 0)
        resp.raise_for_status()
        return self._pr(resp.json())

    def update_pr_body(self, number: int, body: str) -> None:
        self.http.patch(f"/repos/{self.repo}/pulls/{number}", json={"body": body}).raise_for_status()

    def list_prs(self) -> list[PullRequest]:
        resp = self.http.get(f"/repos/{self.repo}/pulls", params={"state": "all", "per_page": 20})
        resp.raise_for_status()
        return [self._pr(item) for item in resp.json()]

    @staticmethod
    def _pr(item: dict) -> PullRequest:
        return PullRequest(item["number"], item["head"]["ref"], item["base"]["ref"], item["title"],
                           item.get("body") or "", item["state"], item["html_url"])


def _force_remove(func, path, _exc) -> None:
    Path(path).chmod(stat.S_IWRITE)
    func(path)
