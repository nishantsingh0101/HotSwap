import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hotswap.config import EXAMPLE_DIR, SCENARIO_DIR  # noqa: E402
from hotswap.gateway.core import AgentInfo, Gateway, NullRoom  # noqa: E402
from hotswap.gateway.remote import LocalRemote  # noqa: E402
from hotswap.gateway.store import Store  # noqa: E402
from hotswap.gitutil import git  # noqa: E402

TASK = json.loads((SCENARIO_DIR / "ledgerlite" / "task.json").read_text())


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.store = Store(tmp / "state.db")
        self.remote = LocalRemote(tmp / "remote.git", self.store)
        self.remote.create(EXAMPLE_DIR, "secret-token")
        self.room = NullRoom()
        self.agents = {
            "builder": AgentInfo("builder", "builder", "hs-builder", "replay", "vendor-a"),
            "standby": AgentInfo("standby", "standby", "hs-standby", "replay", "vendor-b"),
            "verifier": AgentInfo("verifier", "verifier", "hs-verifier", "replay", "vendor-c"),
            "scrubber": AgentInfo("scrubber", "scrubber", "hs-scrubber", "replay", "code"),
        }
        self.gw = Gateway(self.store, self.remote, self.room, TASK, self.agents, tmp / "scratch",
                          drain_seconds=0.5, role_release_seconds=0.5)

    def worktree(self, name: str) -> Path:
        path = self.tmp / "work" / name
        if not path.exists():
            git(self.tmp, "clone", "-q", self.remote.read_url(), str(path), check=True)
        git(path, "fetch", "-q", "origin", check=True)
        branch = self.gw.branch
        if git(path, "rev-parse", "--verify", "-q", f"origin/{branch}").ok:
            git(path, "checkout", "-q", "-B", branch, f"origin/{branch}", check=True)
        else:
            git(path, "checkout", "-q", "-B", branch, "origin/main", check=True)
        return path

    def apply_step(self, path: Path, step: int) -> str:
        source = SCENARIO_DIR / "ledgerlite" / "steps" / str(step)
        for file in source.rglob("*"):
            if file.is_file():
                target = path / file.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
        git(path, "add", "-A", check=True)
        git(path, "commit", "-q", "-m", f"step {step}: replay", check=True)
        return git(path, "rev-parse", "HEAD", check=True).out


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def run():
    def _run(coro):
        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)
    return _run
