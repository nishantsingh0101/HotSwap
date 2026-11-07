"""The verifier: writes tests from the spec first, then checks work it never wrote.

It has no write access to the deliverable. Its tests and mutations run in a
disposable sandbox clone.
"""
from __future__ import annotations

import asyncio
import json

from hotswap.agents.base import AgentProcess, run_agent
from hotswap.config import SCENARIO_DIR
from hotswap.gitutil import commit_all, git, short
from hotswap.verify import Sandbox, mutation_score

TASK = json.loads((SCENARIO_DIR / "ledgerlite" / "task.json").read_text())


class Verifier(AgentProcess):
    role = "verifier"

    def __init__(self) -> None:
        super().__init__()
        self.lock = asyncio.Lock()
        self.box: Sandbox | None = None

    async def sandbox(self) -> Sandbox:
        if self.box is None:
            state = await self.run_state()
            self.box = await asyncio.to_thread(Sandbox, state["read_url"])
        return self.box

    async def handle(self, command: str, args: dict) -> None:
        positional = args.get("_", [])
        if command == "write-tests":
            await self.write_tests(args.get("base"))
        elif command == "checkpoint":
            await self.check(positional[0], "checkpoint")
        elif command == "check-inherited":
            await self.check(positional[0], "inherited")
        elif command == "final":
            await self.final(positional[0], args.get("tests"))

    async def write_tests(self, base: str | None) -> None:
        async with self.lock:
            self.set("working", note="writing acceptance tests from the request", progress=True)
            box = await self.sandbox()
            self.set("working", note="fetching the base repository for spec tests", progress=True)
            await asyncio.to_thread(git, box.path, "fetch", "-q", "origin", check=True)
            await asyncio.to_thread(git, box.path, "checkout", "-q", "-B", f"hotswap/tests/{TASK['id']}", base or "origin/main", check=True)
            self.set("working", note=f"{self.provider.kind}: generating acceptance tests", progress=True)
            files = await self.provider.write_tests(box.path, TASK)
            if not files:
                raise RuntimeError("The provider produced no acceptance tests; implementation cannot start")
            sha = commit_all(box.path, f"acceptance tests for {TASK['id']}, written from the request") or git(box.path, "rev-parse", "HEAD").out
            await self.call("/api/ops/tests_push", {"agent": self.role, "sha": sha, "source": str(box.path), "count": len(files)})
            self.set("ready", note=f"{len(files)} test file(s) at {short(sha)}", progress=True)
            await self.say(f"Wrote acceptance tests from the request before any code exists: `{', '.join(files)}` at `{short(sha)}`.")

    async def run_tests(self, code_sha: str, test_sha: str | None):
        box = await self.sandbox()
        await asyncio.to_thread(box.checkout, code_sha, test_sha)
        return await asyncio.to_thread(box.run_tests)

    async def check(self, code_sha: str, kind: str) -> None:
        async with self.lock:
            run = (await self.run_state())["run"]
            test_sha = run.get("test_sha")
            self.set("working", note=f"checking {short(code_sha)}", progress=True)
            result = await self.run_tests(code_sha, test_sha)
            summary = await self.call("/api/verify/run", {
                "kind": kind, "code_sha": code_sha, "test_sha": test_sha,
                "passed": result.passed, "failed": result.failed, "repo_failed": result.repo_failed,
            })
            self.set("ready", note=f"{kind}: {len(result.passed)} passing at {short(code_sha)}", progress=True)
            if kind == "inherited":
                regressions, pending = summary.get("regressions", []), summary.get("pending", [])
                verdict = "No regressions." if not regressions else f"Regressions: {', '.join(regressions)}."
                await self.say(
                    f"Checked the inherited commits at `{short(code_sha)}`. {verdict} "
                    f"{len(result.passed)} acceptance tests pass; {len(pending)} are pending because their steps are not built yet. "
                    "The partial work is safe to build on."
                )

    async def final(self, code_sha: str, test_sha: str | None) -> None:
        async with self.lock:
            run = (await self.run_state())["run"]
            test_sha = test_sha or run.get("test_sha")
            self.set("working", note=f"final verification of {short(code_sha)}", progress=True)
            result = await self.run_tests(code_sha, test_sha)
            box = await self.sandbox()
            mutation = {"score": 0, "killed": 0, "total": 0}
            if not result.failed and not result.repo_failed:
                self.set("working", note="mutation testing: planting small bugs", progress=True)
                files = await asyncio.to_thread(box.changed_python, run["base_sha"], code_sha)
                mutation = await asyncio.to_thread(mutation_score, box, files)
            await self.call("/api/verify/run", {
                "kind": "final", "code_sha": code_sha, "test_sha": test_sha,
                "passed": result.passed, "failed": result.failed, "repo_failed": result.repo_failed,
            })
            await self.call("/api/verify/attest", {
                "agent": self.role, "code_sha": code_sha, "test_sha": test_sha, "passed": result.passed,
                "failed": result.failed, "repo_failed": result.repo_failed, "mutation": mutation, "command": result.command,
            })
            self.set("done", note=f"verified {short(code_sha)}", progress=True)


if __name__ == "__main__":
    run_agent(Verifier)
