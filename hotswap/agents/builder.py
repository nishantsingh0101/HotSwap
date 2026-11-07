"""The builder, and the standby that takes over from it. Same code, different role."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from hotswap.agents.base import (STEP_COMMIT, AgentProcess, GatewayRefused, crash, recall, remember,
                                 run_agent, worktree_path)
from hotswap.config import SCENARIO_DIR
from hotswap.gitutil import commit_all, git, short

TASK = json.loads((SCENARIO_DIR / "ledgerlite" / "task.json").read_text())

FAULT_STEP = {
    "crash_before_first_commit": 1,
    "after_pr_before_report": TASK["pr_after_step"],
}


class Builder(AgentProcess):
    role = "builder"

    def __init__(self) -> None:
        super().__init__()
        self.lease = recall(self.role).get("lease", 0)
        self.fault: str | None = None
        self.redo: set[int] = set()
        self.worktree = worktree_path(self.role)

    async def main(self) -> None:
        if os.environ.get("HOTSWAP_ZOMBIE") != "1":
            return await super().main()
        # A stale-write probe must NOT replay the interrupted builder command or
        # reacquire its room role. It makes exactly one authenticated old-epoch
        # retry, then stands down. The demo labels this deliberate fault probe.
        beats = asyncio.create_task(self.heartbeat_loop())
        try:
            await self.zombie()
            await asyncio.sleep(8)
        finally:
            beats.cancel()
            await asyncio.gather(beats, return_exceptions=True)

    # Commands ------------------------------------------------------------

    async def handle(self, command: str, args: dict) -> None:
        if command == "start" and self.role == "builder":
            self.lease = int(args.get("lease", 1))
            self.fault = args.get("fault") or None
            remember(self.role, {"lease": self.lease})
            await self.switch.assume_role("builder")
            await self.say(f"Holding the builder role with lease {self.lease}. Starting the plan.")
            await self.build(mode="hotswap", fresh=False)
        elif command == "takeover" and self.role == "standby":
            await self.take_over(int(args["lease"]), args.get("mode", "hotswap"))

    async def startup(self) -> None:
        if os.environ.get("HOTSWAP_ZOMBIE") == "1" and self.role == "builder":
            await self.zombie()

    # Building ------------------------------------------------------------

    def sync(self, branch: str, read_url: str) -> None:
        if not (self.worktree / ".git").exists():
            git(self.worktree.parent, "clone", "-q", read_url, str(self.worktree), check=True)
        git(self.worktree, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", check=True)
        start = f"origin/{branch}" if git(self.worktree, "rev-parse", "--verify", "-q", f"origin/{branch}").ok else "origin/main"
        git(self.worktree, "checkout", "-q", "-B", branch, start, check=True)
        git(self.worktree, "reset", "-q", "--hard", start, check=True)
        git(self.worktree, "clean", "-q", "-fd", check=True)

    def steps_on_branch(self, base: str) -> dict[int, str]:
        """Steps whose commits already exist on the branch, found from commit messages."""
        out = git(self.worktree, "log", "--format=%H %s", f"{base}..HEAD").out
        found = {}
        for line in out.splitlines():
            sha, _, subject = line.partition(" ")
            match = STEP_COMMIT.match(subject)
            if match:
                found.setdefault(int(match.group(1)), sha)
        return found

    async def build(self, mode: str, fresh: bool) -> None:
        state = await self.run_state()
        run = state["run"]
        branch = run["branch"] + ("-fresh" if fresh else "")
        read_url = state["read_url"]
        await asyncio.to_thread(self.sync, branch, read_url)
        base = run["base_sha"]

        earlier = {int(e["step"]) for e in state["worklog"] if e["status"] == "step_done"}
        done = {} if fresh else {int(e["step"]): e for e in state["worklog"] if e["status"] == "step_done"}
        self.branch = branch
        if mode == "hotswap" and not fresh:
            for step, sha in (await asyncio.to_thread(self.steps_on_branch, base)).items():
                if step not in done:
                    await self.op("worklog", step=step, status="step_done", commit=sha, note="found on branch, not repeated")
                    await self.call(f"/api/events/{self.role}", {"kind": "step_adopted", "step": step, "sha": short(sha)})
                    done[step] = {"step": step}

        pr_open = bool(run.get("pr")) and not fresh
        for step in TASK["plan"]:
            n = step["step"]
            if n in done:
                continue
            if n in self.redo or (fresh and n in earlier):
                await self.call(f"/api/events/{self.role}", {"kind": "step_redone", "step": n})
            self.set("working", n, f"step {n}: {step['title']}", progress=True)
            await self.maybe_fault("before_implement", n)
            summary = await self.provider.implement_step(self.worktree, TASK, step)
            self.set("working", n, f"step {n}: testing", progress=True)
            await self.maybe_fault("during_tests", n)
            await asyncio.to_thread(self.quick_tests)
            sha = await asyncio.to_thread(self.commit_step, n, step["title"])
            await self.maybe_fault("after_commit", n)
            if self.fault == "delayed_push" and n == 3:
                asyncio.create_task(self.op("push", sha=sha, step=n, delay=30))
                await asyncio.sleep(1.0)
                crash("died while a push was still in flight")
            result = await self.op("push", sha=sha, step=n)
            if result.get("status") != "done":
                raise RuntimeError(f"push for step {n} was {result.get('status')}: {result.get('reason')}")
            remember(self.role, {"lease": self.lease, "pending": None})
            await self.op("worklog", step=n, status="step_done", commit=sha, note=summary[:120])
            self.set("working", n, f"step {n} checkpointed at {short(sha)}", progress=True)
            if n >= TASK["pr_after_step"] and not pr_open:
                pr = await self.op("open_pr", branch=branch)
                pr_open = True
                await self.maybe_fault("after_pr", n)
                await self.op("worklog", step=n, status="pr_opened", commit=None,
                              note=f"PR #{pr['number']} {'reused' if pr.get('reused') else 'opened'}")
            await self.maybe_fault("after_step", n)

        self.set("done", None, "all steps checkpointed", progress=True)
        await self.call("/api/ops/done", {"agent": self.role, "lease": self.lease})
        await self.say(f"All {len(TASK['plan'])} steps are checkpointed on `{branch}`. Handing over for cleanup and verification.")

    def commit_step(self, n: int, title: str) -> str:
        return commit_all(self.worktree, f"step {n}: {title}") or commit_all(
            self.worktree, f"step {n}: {title} (no file changes)", allow_empty=True)

    def quick_tests(self) -> None:
        """The repository's own tests, run by the writer as a sanity check. Not verification."""
        from hotswap.verify import run_isolated
        result = run_isolated(self.worktree, ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", "--ignore=tests/acceptance"], timeout=180)
        if result.returncode != 0:
            raise RuntimeError("Repository checks failed; checkpoint was not published")

    async def op(self, name: str, **fields) -> dict:
        key = fields.get("step")
        request_id = f"{self.role}:{self.lease}:{name}:{key}:{fields.get('status', '')}"
        payload = {"agent": self.role, "lease": self.lease, "request_id": request_id, **fields}
        if name == "push":
            payload["source"] = str(self.worktree)
            payload["branch"] = getattr(self, "branch", None)
            remember(self.role, {"lease": self.lease, "pending": fields["sha"], "step": key})
        return await self.call(f"/api/ops/{name}", payload)

    async def maybe_fault(self, point: str, step: int) -> None:
        fault = self.fault
        if not fault:
            return
        target = FAULT_STEP.get(fault, 3)
        if step != target:
            return
        if fault == "crash_before_first_commit" and point == "before_implement":
            crash("crashed before the first commit")
        if fault == "crash_after_commit" and point == "after_commit":
            crash("crashed after committing, before pushing")
        if fault == "after_pr_before_report" and point == "after_pr":
            crash("crashed after opening the PR, before reporting it")
        if fault == "crash_during_tests" and point == "during_tests":
            crash("crashed while running tests")
        if fault == "stall" and point == "before_implement":
            self.set("working", step, f"step {step}: thinking", progress=False)
            while True:  # connected and heartbeating, but no progress
                await asyncio.sleep(1)
        if fault == "loop" and point == "before_implement":
            while True:
                self.set("working", step, "retrying the same failing command", error="ImportError: cannot import name 'to_csv'")
                await asyncio.sleep(1.5)

    # Taking over ---------------------------------------------------------

    async def take_over(self, lease: int, mode: str) -> None:
        self.lease = lease
        remember(self.role, {"lease": lease})
        self.set("claiming", note=f"claiming the builder role for lease {lease}")
        for _ in range(60):
            try:
                await self.switch.assume_role("builder")
                break
            except RuntimeError:
                await asyncio.sleep(1)
        else:
            raise RuntimeError("the builder role never became free")
        if mode == "fresh":
            await self.say("Starting this task from scratch on a new branch. No earlier context is available.")
            await self.build(mode=mode, fresh=True)
            return
        await self.call("/api/takeover/claim", {"agent": self.role, "lease": lease})
        if mode == "plain":
            await self.say("Continuing from the room history.")
            await self.build(mode=mode, fresh=False)
            return
        fence = await self.call("/api/ops/fence", {"agent": self.role, "lease": lease})
        resolved = (await self.call("/api/ops/reconcile", {"agent": self.role, "lease": lease}))["items"]
        self.redo = {item.get("step") for item in resolved if item["op"] == "push" and item["status"] == "not_applied"}
        state = await self.run_state()
        done = sorted(int(e["step"]) for e in state["worklog"] if e["status"] == "step_done")
        pr = state["run"].get("pr")
        lines = [
            f"**Taking over as lease {lease}.** Fence commit `{short(fence.get('sha'))}` is on the branch, so any late write from the old builder will fail.",
            f"Inherited: steps {', '.join(map(str, done)) or 'none'} done; last checkpoint `{short(state['run'].get('head'))}`.",
        ]
        for item in resolved:
            if item["op"] == "open_pr":
                lines.append(f"Found PR #{item.get('number')} already open. Continuing it, not opening a new one."
                             if item["status"] == "confirmed" else "No PR was opened yet.")
            elif item["op"] == "push":
                lines.append(f"Step {item.get('step')} push {'landed' if item['status'] == 'confirmed' else 'never landed; it will be redone once'}.")
        if not pr and not any(i["op"] == "open_pr" for i in resolved):
            lines.append("No PR exists yet.")
        lines.append("Waiting for the verifier to check the inherited commits before I build on them.")
        await self.say("\n".join(lines))
        self.set("waiting", note="waiting for the inherited-work check")
        inherited = await self.wait_for_inherited()
        if inherited is None:
            await self.call(f"/api/events/{self.role}", {"kind": "escalate", "reason": "the inherited-work check never arrived; not building on unchecked work"})
            self.set("waiting", note="paused: inherited work was never checked")
            return
        if inherited.get("regressions"):
            await self.call(f"/api/events/{self.role}", {"kind": "escalate", "reason": f"inherited regressions: {inherited['regressions']}"})
            return
        await self.build(mode="hotswap", fresh=False)

    async def wait_for_inherited(self, seconds: int = 90) -> dict | None:
        for _ in range(seconds):
            run = (await self.run_state())["run"]
            if run.get("inherited"):
                return run["inherited"]
            await asyncio.sleep(1)
        return None

    async def zombie(self) -> None:
        """The old builder comes back and tries to push the work it was holding."""
        memory = recall(self.role)
        await asyncio.sleep(2)
        self.set("zombie", note=f"back with stale lease {memory.get('lease')}")
        await self.say(f"Back online. Pushing my last commit with lease {memory.get('lease')}.")
        sha = memory.get("pending") or git(self.worktree, "rev-parse", "HEAD").out
        try:
            await self.call("/api/ops/push", {
                "agent": self.role, "lease": memory.get("lease", 1), "request_id": f"zombie:{sha}",
                "sha": sha, "source": str(self.worktree), "step": memory.get("step"),
            })
        except GatewayRefused as refused:
            self.set("fenced", note=refused.reason)
            await self.say(f"The gateway refused my write: {refused.reason}. Standing down.")


class Standby(Builder):
    role = "standby"


if __name__ == "__main__":
    run_agent(Standby if "standby" in sys.argv[1:] else Builder)
