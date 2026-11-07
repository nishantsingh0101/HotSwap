"""Small, explicit wrappers around the git command line."""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Hot Swap",
    "GIT_AUTHOR_EMAIL": "hotswap@localhost",
    "GIT_COMMITTER_NAME": "Hot Swap",
    "GIT_COMMITTER_EMAIL": "hotswap@localhost",
    "GIT_TERMINAL_PROMPT": "0",
}


@dataclass
class GitResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def _env(extra: dict | None = None) -> dict:
    env = dict(os.environ, **GIT_ENV)
    if extra:
        env.update(extra)
    return env


def git(cwd: str | Path, *args: str, extra_env: dict | None = None, check: bool = False) -> GitResult:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_env(extra_env), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    result = GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    if check and not result.ok:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.err or result.out}")
    return result


async def git_async(cwd: str | Path, *args: str, extra_env: dict | None = None) -> GitResult:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), env=_env(extra_env),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return GitResult(proc.returncode, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip())


def head(cwd: str | Path) -> str:
    return git(cwd, "rev-parse", "HEAD", check=True).out


def short(sha: str | None) -> str:
    return (sha or "")[:7]


def changed_files(cwd: str | Path, base: str, tip: str = "HEAD") -> list[str]:
    out = git(cwd, "diff", "--name-only", f"{base}..{tip}", check=True).out
    return [line for line in out.splitlines() if line]


def commit_all(cwd: str | Path, message: str, allow_empty: bool = False) -> str | None:
    git(cwd, "add", "-A", check=True)
    status = git(cwd, "status", "--porcelain").out
    if not status and not allow_empty:
        return None
    args = ["commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    git(cwd, *args, check=True)
    return head(cwd)
