"""The write gateway: every write by every agent goes through here.

It owns the lease number, the takeover barrier, the intent log, the work log,
verification records and the responsibility ledger. Models never decide any of
this; this module does.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hotswap.gitutil import git, git_async, short
from hotswap.gateway.remote import PullRequestExists, Remote
from hotswap.gateway.store import Store
from hotswap.runtime import stop_process_tree

TASK_BRANCH = "hotswap/{task}"
BUILDERS = ("builder", "standby")


class Room(Protocol):
    async def post(self, text: str, as_role: str = "gateway") -> None: ...
    async def targeted(self, names: list[str], text: str) -> None: ...
    async def role_holder(self) -> str | None: ...
    async def write_doc(self, title: str, content: str) -> None: ...


class NullRoom:
    """Used in tests and when Switch is not connected. Records what would be posted."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.targets: list[tuple[list[str], str]] = []
        self.holder: str | None = None
        self.docs: dict[str, str] = {}

    async def post(self, text: str, as_role: str = "gateway") -> None:
        self.posts.append(text)

    async def targeted(self, names: list[str], text: str) -> None:
        self.targets.append((names, text))

    async def role_holder(self) -> str | None:
        return self.holder

    async def write_doc(self, title: str, content: str) -> None:
        self.docs[title] = content


class Refused(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class AgentInfo:
    name: str
    role: str
    switch_name: str
    provider: str = "replay"
    vendor: str = "replay"


@dataclass
class Gateway:
    store: Store
    remote: Remote
    room: Room
    task: dict
    agents: dict[str, AgentInfo]
    scratch: Path
    drain_seconds: float = 5.0
    role_release_seconds: float = 30.0
    faults_enabled: bool = True
    inflight: dict[int, int] = field(default_factory=dict)
    listeners: list[asyncio.Queue] = field(default_factory=list)
    takeover_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def recover_after_restart(self) -> bool:
        """Revoke interrupted authority and preserve evidence for operator recovery.

        A persisted phase is not proof that an external operation completed.
        Startup must not quietly re-execute it or reset the ledger.
        """
        status = self.run("status")
        if not status or status in ("accepted", "rejected", "blocked", "escalated"):
            return False
        lease = self.store.lease()
        self.set_run("interrupted", {"status": status, "lease": lease})
        self.store.set_lease(lease["number"] + 1, None, "blocked")
        self.set_run("status", "blocked")
        for intent in self.store.intents("pending"):
            self.store.finish_intent(intent["request_id"], "unknown")
        self.emit("restart_blocked", previous_status=status, previous_phase=lease["phase"],
                  reason="Gateway restarted during work. Authority revoked; reconcile before operator resume.")
        return True

    # Events -------------------------------------------------------------

    def emit(self, kind: str, **data: Any) -> dict:
        event = self.store.add_event(kind, data)
        for queue in list(self.listeners):
            queue.put_nowait(event)
        return event

    async def say(self, text: str, as_role: str = "gateway") -> None:
        try:
            await self.room.post(text, as_role)
        except Exception as error:  # the room is a view; a failed post must not break recovery
            self.emit("room_error", error=str(error))

    async def tell(self, role: str, text: str) -> None:
        try:
            await self.room.targeted([self.agents[role].switch_name], text)
        except Exception as error:
            self.emit("room_error", error=str(error))
            self.block_for_recovery("Switch command delivery is uncertain; reconcile before continuing")
            raise Refused("room_delivery_uncertain", "Switch command delivery failed. Work is preserved and writes are blocked.") from error

    def block_for_recovery(self, reason: str) -> None:
        if not self.run("interrupted"):
            self.set_run("interrupted", {"status": self.run("status"), "lease": self.store.lease()})
        lease = self.store.lease()
        self.store.set_lease(lease["number"] + 1, None, "blocked")
        self.set_run("status", "blocked")
        self.emit("recovery_blocked", reason=reason)

    # Run state ----------------------------------------------------------

    @property
    def branch(self) -> str:
        return TASK_BRANCH.format(task=self.task["id"])

    def run(self, key: str, default: Any = None) -> Any:
        return self.store.get(f"run.{key}", default)

    def set_run(self, key: str, value: Any) -> None:
        self.store.put(f"run.{key}", value)

    @property
    def mode(self) -> str:
        return self.run("mode", "hotswap")

    def requirements_version(self) -> str:
        body = json.dumps({"request": self.task["request"], "acceptance": self.task["acceptance"]}, sort_keys=True)
        return "req-" + hashlib.sha256(body.encode()).hexdigest()[:10]

    async def start_task(self, mode: str = "hotswap", fault: str | None = None) -> dict:
        if mode not in ("hotswap", "plain", "fresh"):
            raise Refused("bad_mode", f"unknown mode {mode}")
        epoch = self.store.lease()["number"] + 1
        self.store.reset_run()
        base = await asyncio.to_thread(self.remote.branch_head, "main")
        self.set_run("mode", mode)
        self.set_run("fault", fault)
        self.set_run("base_sha", base)
        self.set_run("branch", self.branch)
        self.set_run("req_version", self.requirements_version())
        self.set_run("started_at", time.time())
        self.set_run("status", "writing_tests")
        self.store.set_lease(epoch, self.agents["builder"].name, "active")
        self.emit(
            "task_started", task=self.task["id"], title=self.task["title"], mode=mode, fault=fault,
            base=base, branch=self.branch, req_version=self.requirements_version(),
            plan=[{"step": s["step"], "title": s["title"]} for s in self.task["plan"]],
        )
        await self.say(
            f"**New task: {self.task['title']}**\n{self.task['request']}\n\n"
            f"Requirements version `{self.requirements_version()}`. Mode `{mode}`. Lease {epoch} goes to the builder."
        )
        await self.publish_worklog()
        await self.tell("verifier", f"write-tests {self.task['id']} base={base}")
        return self.state()

    # Heartbeats ---------------------------------------------------------

    def heartbeat(self, name: str, pid: int, state: str, step: int | None, note: str,
                  progress: bool, error: str | None) -> dict:
        info = self.agents[name]
        now = time.time()
        row = self.store.one("SELECT * FROM agents WHERE name = ?", (name,))
        errors = json.loads(row["errors"]) if row else []
        if error:
            errors = (errors + [error])[-5:]
        if row is None:
            self.store.execute(
                "INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, info.role, info.provider, info.vendor, pid, state, step, note, now, now if progress else None,
                 json.dumps(errors)),
            )
        else:
            self.store.execute(
                "UPDATE agents SET pid = ?, state = ?, step = ?, note = ?, last_heartbeat = ?, "
                "last_progress = COALESCE(?, last_progress), errors = ? WHERE name = ?",
                (pid, state, step, note, now, now if progress else None, json.dumps(errors), name),
            )
        if progress or error or (row and row["state"] != state):
            self.emit("agent", name=name, state=state, step=step, note=note, error=error)
        return self.store.lease()

    def touch_progress(self, name: str) -> None:
        self.store.execute("UPDATE agents SET last_progress = ? WHERE name = ?", (time.time(), name))

    # Lease admission ----------------------------------------------------

    def admit(self, agent: str, lease: int, op: str, phases: tuple[str, ...] = ("active",)) -> dict:
        current = self.store.lease()
        if self.mode != "hotswap":
            return current  # plain and fresh modes run without lease protection, for comparison
        if lease != current["number"]:
            self.emit("stale_write_rejected", agent=agent, op=op, lease=lease, current=current["number"], layer="gateway")
            raise Refused("stale_lease", f"stale lease {lease}, current lease {current['number']}")
        if current["phase"] not in phases:
            raise Refused("wrong_phase", f"{op} is not allowed while the lease is {current['phase']}")
        if agent != current["holder"]:
            raise Refused("not_holder", f"{agent} does not hold lease {lease}")
        return current

    def _begin(self, request_id: str, lease: int, agent: str, op: str, payload: dict) -> dict | None:
        existing = self.store.intent(request_id)
        if existing:
            return existing
        self.store.add_intent(request_id, lease, agent, op, payload)
        self.inflight[lease] = self.inflight.get(lease, 0) + 1
        return None

    def _end(self, lease: int) -> None:
        self.inflight[lease] = max(0, self.inflight.get(lease, 0) - 1)

    # Supported operations ----------------------------------------------

    async def push(self, agent: str, lease: int, request_id: str, sha: str, source: str,
                   step: int | None, delay: float = 0.0, branch: str | None = None) -> dict:
        target = branch or self.branch
        existing = self.store.intent(request_id)
        if existing:
            same = (existing["op"], existing["agent"], existing["lease"], existing["payload"].get("sha"), existing["payload"].get("step"), existing["payload"].get("branch")) == ("push", agent, lease, sha, step, target)
            if not same:
                raise Refused("request_conflict", "this request id was already used for a different write")
            return {"replayed": True, **(existing["result"] or {"status": existing["status"]})}
        self.admit(agent, lease, "push")
        if self.mode == "hotswap" and target != self.branch:
            raise Refused("wrong_branch", f"writes go to {self.branch}")
        expected = await asyncio.to_thread(self.remote.branch_head, target)
        # The lookup yields to takeover. Never pair a revoked writer with the
        # replacement's freshly-read branch head. Admission + intent registration
        # below have no intervening await on this event loop.
        self.admit(agent, lease, "push")
        if self.store.intent(request_id):
            # A concurrent identical request may have registered during lookup.
            # Re-enter the validated replay path; never dispatch it twice.
            return await self.push(agent, lease, request_id, sha, source, step, delay, branch)
        self._begin(request_id, lease, agent, "push", {"sha": sha, "step": step, "expected": expected, "branch": target})
        try:
            if delay and self.faults_enabled:
                self.emit("push_delayed", agent=agent, lease=lease, sha=sha, seconds=delay)
                await asyncio.sleep(delay)
            if self.mode == "hotswap":
                result = await self.remote.push(Path(source), sha, target, expected)
            else:
                result = await self._force_push(Path(source), sha, target)
            current = self.store.lease()["number"]
            if result.ok:
                outcome = {"status": "done", "sha": sha, "step": step}
                self.store.finish_intent(request_id, "done", outcome)
                if target == self.branch or self.mode == "fresh":
                    self.set_run("head", sha)
                failover = self.run("failover")
                if failover and not failover.get("ended") and lease == failover.get("new"):
                    failover["ended"] = time.time()
                    self.set_run("failover", failover)
                if lease < current and (self.mode != "hotswap" or self.run("fenced")):
                    self.emit("stale_write_accepted", agent=agent, lease=lease, current=current, sha=short(sha))
                elif lease < current:
                    self.emit("drained_write_landed", agent=agent, lease=lease, sha=short(sha))
                self.emit("checkpoint", agent=agent, lease=lease, sha=short(sha), full_sha=sha, step=step)
                self.touch_progress(agent)
                await self.tell("verifier", f"checkpoint {sha}")
                return outcome
            outcome = {"status": "rejected", "reason": result.err.splitlines()[-1] if result.err else "rejected"}
            self.store.finish_intent(request_id, "rejected", outcome)
            if lease < current:
                self.emit("stale_write_rejected", agent=agent, op="push", lease=lease, current=current,
                          layer="remote", detail="branch head moved by the fence commit")
                await self.say(f"Rejected a late push from `{agent}` (lease {lease}). The branch moved on; nothing was overwritten.")
            else:
                self.emit("push_rejected", agent=agent, lease=lease, reason=outcome["reason"])
            return outcome
        finally:
            self._end(lease)

    async def _force_push(self, source: Path, sha: str, branch: str):
        return await git_async(
            source, "push", "--force", "--porcelain", self.remote.write_url(), f"{sha}:refs/heads/{branch}",
            extra_env=self.remote.push_env(),
        )

    async def open_pr(self, agent: str, lease: int, request_id: str, branch: str | None = None) -> dict:
        existing = self.store.intent(request_id)
        if existing:
            if (existing["agent"], existing["lease"]) != (agent, lease):
                raise Refused("request_conflict", "this request id was already used by another writer")
            if existing["result"]:
                return {"replayed": True, **existing["result"]}
        self.admit(agent, lease, "open_pr")
        head = branch or self.branch
        self._begin(request_id, lease, agent, "open_pr", {"head": head})
        try:
            try:
                pr = await asyncio.to_thread(self.remote.create_pr, head, "main", self.task["title"], self.render_pr_body(), agent)
                outcome = {"status": "done", "number": pr.number, "url": pr.url, "reused": False, "head": head}
                prs = self.run("prs", [])
                self.set_run("prs", prs + [pr.number])
                self.emit("pr_opened", agent=agent, number=pr.number, url=pr.url, head=head)
            except PullRequestExists as exists:
                outcome = {"status": "done", "number": exists.number, "reused": True, "head": head}
                self.emit("pr_reused", agent=agent, number=exists.number, head=head)
            self.store.finish_intent(request_id, "done", outcome)
            if head == self.branch:
                self.set_run("pr", outcome["number"])
            self.touch_progress(agent)
            return outcome
        finally:
            self._end(lease)

    async def append_worklog(self, agent: str, lease: int, request_id: str, step: int | None,
                             status: str, commit: str | None, note: str) -> dict:
        prior = self.store.one("SELECT agent, lease, step, status FROM worklog WHERE request_id = ?", (request_id,))
        if prior:
            if (prior["agent"], prior["lease"], prior["step"], prior["status"]) != (agent, lease, step, status):
                raise Refused("request_conflict", "this request id was already used for a different entry")
            return {"status": "done", "replayed": True}
        self.admit(agent, lease, "worklog")
        self._begin(request_id, lease, agent, "worklog", {"step": step, "status": status})
        try:
            self.store.execute(
                "INSERT INTO worklog (request_id, lease, agent, step, status, commit_sha, note, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, lease, agent, step, status, commit, note, time.time()),
            )
            self.store.finish_intent(request_id, "done", {"status": "done"})
            self.emit("worklog", agent=agent, lease=lease, step=step, status=status, commit=short(commit), note=note)
            self.touch_progress(agent)
            await self.publish_worklog()
            return {"status": "done"}
        finally:
            self._end(lease)

    # Work log and PR body ------------------------------------------------

    def worklog(self) -> list[dict]:
        return self.store.all("SELECT * FROM worklog ORDER BY seq")

    def completed_steps(self) -> dict[int, dict]:
        done: dict[int, dict] = {}
        for entry in self.worklog():
            if entry["status"] == "step_done" and entry["step"] is not None:
                done[entry["step"]] = entry
        return done

    def render_worklog(self) -> str:
        done = self.completed_steps()
        lease = self.store.lease()
        lines = [
            f"# Work log: {self.task['title']}",
            f"Branch `{self.branch}` | lease {lease['number']} held by {lease['holder'] or 'nobody'} ({lease['phase']})",
            f"Requirements `{self.run('req_version', self.requirements_version())}` | tests `{short(self.run('test_sha'))}`",
            "",
        ]
        for step in self.task["plan"]:
            entry = done.get(step["step"])
            mark = f"done by {entry['agent']} at `{short(entry['commit_sha'])}`" if entry else "pending"
            lines.append(f"{step['step']}. {step['title']}: {mark}")
        pr = self.run("pr")
        lines += ["", f"Pull request: #{pr}" if pr else "Pull request: not opened yet"]
        return "\n".join(lines)

    def render_pr_body(self) -> str:
        return self.render_worklog() + "\n\n_This description is generated from the work log by the Hot Swap gateway._"

    async def publish_worklog(self) -> None:
        text = self.render_worklog()
        try:
            await self.room.write_doc("Hot Swap work log", text)
        except Exception as error:
            self.emit("room_error", error=str(error))
        pr = self.run("pr")
        if pr:
            try:
                await asyncio.to_thread(self.remote.update_pr_body, pr, self.render_pr_body())
            except Exception as error:
                self.emit("pr_error", error=str(error))

    # Takeover ------------------------------------------------------------

    async def takeover(self, failed: str, reason: str) -> None:
        async with self.takeover_lock:
            current = self.store.lease()
            if current["holder"] != failed or current["phase"] != "active":
                return
            if failed == self.agents["standby"].name:
                await self.escalate(f"The standby failed as well ({reason}). Work is paused with nothing lost.")
                return
            old = current["number"]
            new = old + 1
            started = time.time()
            self.set_run("failover", {"failed": failed, "reason": reason, "old": old, "new": new, "started": started})
            self.set_run("status", "recovering")
            if self.mode == "hotswap":
                self.store.set_lease(new, None, "suspended")
            else:
                self.store.set_lease(new, self.agents["standby"].name, "active")
            self.emit("failover_started", failed=failed, reason=reason, old=old, new=new, mode=self.mode)
            await self.say(f"**Failover.** `{failed}` {reason}. Lease {old} is revoked; starting recovery as lease {new}.",
                           as_role="watchdog")

            if self.mode == "hotswap":
                deadline = time.time() + self.drain_seconds
                while self.inflight.get(old, 0) > 0 and time.time() < deadline:
                    await asyncio.sleep(0.1)
                unknown = []
                for intent in self.store.intents("pending"):
                    if intent["lease"] == old:
                        self.store.finish_intent(intent["request_id"], "unknown")
                        unknown.append(intent["op"])
                self.emit("drained", lease=old, unknown=unknown)

            self.kill_agent(failed)
            self.store.set_lease(phase="claiming") if self.mode == "hotswap" else None
            self.emit("role_release", agent=failed)
            released = await self._wait_role_free(self.agents[failed].switch_name)
            self.emit("role_released" if released else "role_release_slow", agent=failed)
            if not released:
                self.block_for_recovery("The old Switch role could not be confirmed released")
                return
            await self.tell("standby", f"takeover lease={new} mode={self.mode}")

    async def _wait_role_free(self, switch_name: str) -> bool:
        deadline = time.time() + self.role_release_seconds
        while True:
            try:
                holder = await self.room.role_holder()
            except Exception:
                if time.time() >= deadline:
                    return False
                await asyncio.sleep(min(.5, max(0, deadline - time.time())))
                continue
            if holder != switch_name:
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(min(.5, max(0, deadline - time.time())))

    async def claim(self, agent: str, lease: int) -> dict:
        current = self.store.lease()
        if self.mode != "hotswap":
            self.emit("claim_confirmed", agent=agent, lease=current["number"])
            return current
        if lease != current["number"] or current["phase"] != "claiming":
            raise Refused("not_claimable", f"lease {lease} is not open for claiming ({current['phase']})")
        holder = await self.room.role_holder()
        if holder != self.agents[agent].switch_name:
            raise Refused("role_not_held", f"Switch reports the builder role is held by {holder or 'nobody'}")
        self.store.set_lease(holder=agent, phase="fencing")
        self.emit("claim_confirmed", agent=agent, lease=lease, switch_holder=holder)
        return self.store.lease()

    async def fence(self, agent: str, lease: int) -> dict:
        if self.mode != "hotswap":
            return {"status": "skipped"}
        self.admit(agent, lease, "fence", phases=("fencing",))
        expected = await asyncio.to_thread(self.remote.branch_head, self.branch)
        if expected is None:
            # Nothing was pushed yet. Create the branch with the fence commit on top of the base, so a
            # late first push from the old builder (which expects no branch) is refused by the server.
            base = self.run("base_sha")
            sha = await asyncio.to_thread(self._fence_commit, base, lease)
            result = await self.remote.push(self.scratch, sha, self.branch, None)
            if not result.ok:
                expected = await asyncio.to_thread(self.remote.branch_head, self.branch)
                if expected is None:
                    raise Refused("fence_failed", result.err)
            else:
                self.set_run("head", sha)
                self.set_run("fence_sha", sha)
                self.set_run("fenced", True)
                self.store.set_lease(phase="active")
                self.emit("fenced", agent=agent, lease=lease, sha=short(sha), previous=None)
                summary = {"run": "inherited", "code": short(sha), "passed": 0, "failed": 0, "regressions": [], "pending": []}
                self.set_run("inherited", summary)
                self.emit("tests_ran", **summary)
                return {"status": "done", "sha": sha}
        for _attempt in range(4):
            sha = await asyncio.to_thread(self._fence_commit, expected, lease)
            result = await self.remote.push(self.scratch, sha, self.branch, expected)
            if result.ok:
                break
            # An old-lease write admitted before the takeover landed first. That write is
            # legitimate; fence on top of it.
            expected = await asyncio.to_thread(self.remote.branch_head, self.branch)
        else:
            raise Refused("fence_failed", result.err)
        self.set_run("head", sha)
        self.set_run("fence_sha", sha)
        self.set_run("fenced", True)
        self.store.set_lease(phase="active")
        self.emit("fenced", agent=agent, lease=lease, sha=short(sha), previous=short(expected))
        await self.tell("verifier", f"check-inherited {sha}")
        return {"status": "done", "sha": sha}

    async def reconcile(self, agent: str, lease: int) -> list[dict]:
        self.admit(agent, lease, "reconcile")
        resolved = await asyncio.to_thread(self._reconcile, lease)
        self.emit("reconciled", agent=agent, items=resolved)
        return resolved

    def _reconcile(self, lease: int) -> list[dict]:
        resolved = []
        reported = {entry["status"] for entry in self.worklog()}
        for intent in self.store.intents():
            if intent["lease"] >= lease:
                continue
            if intent["status"] == "done" and intent["op"] == "open_pr" and "pr_opened" not in reported:
                number = (intent["result"] or {}).get("number")
                resolved.append({"op": "open_pr", "status": "confirmed", "number": number, "unreported": True})
                continue
            if intent["status"] not in ("pending", "unknown"):
                continue
            op, payload = intent["op"], intent["payload"]
            if op == "push":
                applied = self._branch_contains(payload["sha"])
                status = "confirmed" if applied else "not_applied"
            elif op == "open_pr":
                pr = self.remote.find_open_pr(payload["head"], "main")
                applied = pr is not None
                status = "confirmed" if applied else "not_applied"
                if pr:
                    self.set_run("pr", pr.number)
                    payload["number"] = pr.number
            else:
                applied = self.store.one("SELECT 1 FROM worklog WHERE request_id = ?", (intent["request_id"],)) is not None
                status = "confirmed" if applied else "not_applied"
            self.store.finish_intent(intent["request_id"], "done" if applied else "not_applied", {"reconciled": status, **payload})
            resolved.append({"op": op, "status": status, **payload})
        return resolved

    def _fence_commit(self, expected: str, lease: int) -> str:
        self._sync_scratch()
        git(self.scratch, "checkout", "-q", "-B", "fence", expected, check=True)
        git(self.scratch, "commit", "-q", "--allow-empty", "-m", f"hotswap: takeover fence, lease {lease}", check=True)
        return git(self.scratch, "rev-parse", "HEAD", check=True).out

    def _sync_scratch(self) -> None:
        if not (self.scratch / ".git").exists():
            self.scratch.parent.mkdir(parents=True, exist_ok=True)
            git(self.scratch.parent, "clone", "-q", self.remote.read_url(), str(self.scratch), check=True)
        git(self.scratch, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", check=True)

    def _branch_contains(self, sha: str) -> bool:
        self._sync_scratch()
        tip = self.remote.branch_head(self.branch)
        if not tip:
            return False
        return git(self.scratch, "merge-base", "--is-ancestor", sha, tip).ok

    def kill_agent(self, name: str) -> bool:
        row = self.store.one("SELECT pid FROM agents WHERE name = ?", (name,))
        if not row or not row["pid"]:
            return False
        try:
            stop_process_tree(row["pid"])
            self.emit("agent_stopped", name=name, pid=row["pid"])
            return True
        except OSError:
            return False

    async def escalate(self, reason: str) -> None:
        self.set_run("status", "escalated")
        self.emit("escalated", reason=reason)
        await self.say(f"**Needs a person.** {reason}")

    # Builder finished, scrub, verify, accept -----------------------------

    async def builder_done(self, agent: str, lease: int) -> None:
        self.admit(agent, lease, "done")
        self.set_run("built_at", time.time())
        self.set_run("status", "scrubbing")
        self.store.set_lease(phase="scrubbing")
        self.emit("build_complete", agent=agent, head=short(self.run("head")))
        await self.tell("scrubber", f"scrub base={self.run('base_sha')} head={self.run('head')}")

    def added_lines(self) -> dict[str, list[str]]:
        self._sync_scratch()
        base, tip = self.run("base_sha"), self.run("head")
        diff = git(self.scratch, "diff", "--unified=0", f"{base}..{tip}", check=True).out
        added: dict[str, list[str]] = {}
        current = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else None
            elif line.startswith("+") and not line.startswith("+++") and current:
                added.setdefault(current, []).append(line[1:])
        return added

    async def apply_scrub(self, proposals: list[dict]) -> dict:
        if self.store.lease()["phase"] != "scrubbing":
            raise Refused("wrong_phase", "scrubbing is only allowed after the build completes")
        base, tip = self.run("base_sha"), self.run("head")
        plan_files = {f for step in self.task["plan"] for f in step["files"]}
        added = await asyncio.to_thread(self.added_lines)
        await asyncio.to_thread(git, self.scratch, "checkout", "-q", "-B", "scrub", tip)
        applied, skipped = [], []
        for proposal in proposals:
            path = proposal["file"]
            if path.startswith("tests/"):
                skipped.append({**proposal, "why": "tests are never changed by the scrubber"})
                continue
            target = self.scratch / path
            if proposal["kind"] == "revert_file":
                if path in plan_files:
                    skipped.append({**proposal, "why": "file is part of the plan"})
                    continue
                in_base = git(self.scratch, "cat-file", "-e", f"{base}:{path}").ok
                if in_base:
                    git(self.scratch, "checkout", "-q", base, "--", path, check=True)
                elif target.exists():
                    target.unlink()
                applied.append(proposal)
            elif proposal["kind"] == "remove_line":
                text = proposal["text"]
                if text not in added.get(path, []) or not target.exists():
                    skipped.append({**proposal, "why": "line was not added by this task"})
                    continue
                lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
                for index, line in enumerate(lines):
                    if line.rstrip("\r\n") == text:
                        del lines[index]
                        break
                target.write_text("".join(lines), encoding="utf-8", newline="")
                applied.append(proposal)
            elif proposal["kind"] == "remove_block":
                block = self._block(target, proposal["text"]) if target.exists() else None
                if block is None or any(line.strip() and line not in added.get(path, []) for line in block[2]):
                    skipped.append({**proposal, "why": "block was not entirely added by this task"})
                    continue
                start, end, _ = block
                lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
                del lines[start:end]
                target.write_text("".join(lines), encoding="utf-8", newline="")
                applied.append(proposal)
        if not applied:
            self.emit("scrub_applied", applied=[], skipped=skipped, sha=None)
            await self.request_final_verification()
            return {"applied": [], "skipped": skipped}
        git(self.scratch, "add", "-A", check=True)
        git(self.scratch, "commit", "-q", "-m", f"scrub: remove {len(applied)} pieces of leftover debris", check=True)
        sha = git(self.scratch, "rev-parse", "HEAD", check=True).out
        result = await self.remote.push(self.scratch, sha, self.branch, tip)
        if not result.ok:
            raise Refused("scrub_push_failed", result.err)
        self.set_run("head", sha)
        self.set_run("scrub", {"applied": applied, "skipped": skipped, "sha": sha})
        self.emit("scrub_applied", applied=applied, skipped=skipped, sha=short(sha))
        await self.publish_worklog()
        await self.request_final_verification()
        return {"applied": applied, "skipped": skipped, "sha": sha}

    @staticmethod
    def _block(target: Path, header: str) -> tuple[int, int, list[str]] | None:
        """Finds a top-level def by its header line; returns [start, end) and the block's lines."""
        lines = target.read_text(encoding="utf-8").splitlines()
        try:
            start = lines.index(header)
        except ValueError:
            return None
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        # take the blank lines that follow too, so spacing stays tidy
        trail = end
        while trail < len(lines) and not lines[trail].strip():
            trail += 1
        return start, trail, lines[start:end]

    async def request_final_verification(self) -> None:
        self.set_run("status", "verifying")
        self.store.set_lease(phase="verifying")
        await self.tell("verifier", f"final {self.run('head')} tests={self.run('test_sha')}")

    def record_tests_pushed(self, agent: str, sha: str, count: int) -> None:
        if agent != self.agents["verifier"].name:
            raise Refused("not_verifier", "only the verifier publishes acceptance tests")
        if count < 1:
            raise Refused("empty_tests", "acceptance-test files are required before implementation")
        self.set_run("test_sha", sha)
        self.set_run("status", "building")
        self.emit("tests_written", agent=agent, sha=short(sha), full_sha=sha, count=count)

    def record_test_run(self, kind: str, code_sha: str, test_sha: str | None, passed: list[str],
                        failed: list[str], repo_failed: list[str], detail: str = "") -> dict:
        self.store.execute(
            "INSERT INTO test_runs (kind, code_sha, test_sha, passed, failed, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, code_sha, test_sha, json.dumps(passed), json.dumps(failed + repo_failed), detail, time.time()),
        )
        summary = {"run": kind, "code": short(code_sha), "passed": len(passed), "failed": len(failed) + len(repo_failed)}
        if kind == "inherited":
            ever_passed: set[str] = set()
            for row in self.store.all("SELECT passed FROM test_runs WHERE kind = 'checkpoint'"):
                ever_passed |= set(json.loads(row["passed"]))
            regressions = sorted(set(failed) & ever_passed) + sorted(repo_failed)
            pending = sorted(set(failed) - ever_passed)
            summary.update(regressions=regressions, pending=pending)
            self.set_run("inherited", summary)
        self.emit("tests_ran", **summary)
        return summary

    async def attest(self, agent: str, code_sha: str, test_sha: str, passed: list[str], failed: list[str],
                     repo_failed: list[str], mutation: dict, command: str) -> dict:
        verifier = self.agents["verifier"]
        if agent != verifier.name:
            raise Refused("not_verifier", "only the verifier can attest")
        holder = self.store.lease()["holder"]
        writer_vendor = self.agents[holder].vendor if holder in self.agents else None
        if writer_vendor and writer_vendor == verifier.vendor:
            self.set_run("status", "blocked")
            self.emit("acceptance_blocked", reason=f"verifier and writer share provider {writer_vendor}")
            await self.say("Final acceptance is blocked: no verifier from a different provider is available.")
            raise Refused("same_vendor", "verifier must use a different provider from the writer")
        if code_sha != self.run("head"):
            raise Refused("stale_verification", "the branch moved after this verification started")
        if not test_sha or test_sha != self.run("test_sha"):
            raise Refused("wrong_tests", "attestation must use the pinned acceptance-test commit")
        recorded = self.store.one(
            "SELECT passed, failed FROM test_runs WHERE kind = 'final' AND code_sha = ? AND test_sha = ? ORDER BY id DESC LIMIT 1",
            (code_sha, test_sha),
        )
        if recorded is None:
            raise Refused("no_test_run", "no recorded final test run matches this attestation")
        if sorted(json.loads(recorded["passed"])) != sorted(passed) or json.loads(recorded["failed"]) != failed + repo_failed:
            raise Refused("evidence_mismatch", "attested results differ from the recorded test run")
        if not passed and not failed:
            raise Refused("no_tests", "no acceptance tests ran, so nothing was verified")
        if failed or repo_failed:
            self.set_run("status", "rejected")
            self.emit("verification_failed", failed=failed + repo_failed)
            await self.say(f"Verification failed on `{short(code_sha)}`: {', '.join(failed + repo_failed)}")
            return {"status": "failed"}
        authors = sorted({row["agent"] for row in self.store.all("SELECT agent FROM worklog WHERE status = 'step_done'")})
        evidence = {"command": command, "passed": passed, "mutation": mutation}
        self.store.execute(
            "INSERT INTO ledger (code_sha, test_sha, req_version, authors, attested_by, evidence, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code_sha, test_sha, self.run("req_version"), json.dumps(authors), agent, json.dumps(evidence), time.time()),
        )
        self.set_run("attested", {"code_sha": code_sha, "test_sha": test_sha, "req_version": self.run("req_version")})
        self.set_run("status", "awaiting_acceptance")
        self.store.set_lease(phase="awaiting_acceptance")
        self.emit("attested", code=short(code_sha), tests=short(test_sha), passed=len(passed),
                  mutation=mutation, authors=authors, attested_by=agent)
        await self.say(
            f"**Verified** `{short(code_sha)}` against tests `{short(test_sha)}` and requirements `{self.run('req_version')}`: "
            f"{len(passed)} acceptance tests pass, mutation score {mutation.get('score', 0)}%.\n"
            f"Owner: reply `accept {short(code_sha)}` to accept this exact commit."
        )
        return {"status": "attested"}

    async def handle_human_message(self, sender: str, body: str) -> None:
        """Counts people's messages during recovery and handles acceptance."""
        failover = self.run("failover")
        if failover and not failover.get("ended"):
            self.set_run("human_messages_during_recovery", self.run("human_messages_during_recovery", 0) + 1)
        match = re.search(r"\baccept\s+([0-9a-f]{7,40})\b", body)
        if match:
            await self.accept(sender, match.group(1))

    async def accept(self, sender: str, sha_prefix: str) -> dict:
        owner = self.store.get("owner_sender")
        attested = self.run("attested")
        if sender != owner:
            self.emit("acceptance_refused", reason="not the owner", sender=sender)
            await self.say("Only the owner can accept this work. Nothing was changed.")
            raise Refused("not_owner", "only the owner can accept")
        if not attested:
            raise Refused("nothing_attested", "there is no verified commit to accept")
        if not attested["code_sha"].startswith(sha_prefix) or attested["code_sha"] != self.run("head"):
            self.emit("acceptance_refused", reason="stale or wrong commit", sender=sender)
            await self.say(f"`{sha_prefix}` is not the verified final commit. Accept `{short(self.run('head'))}` instead.")
            raise Refused("stale_acceptance", "that is not the verified final commit")
        self.store.execute(
            "UPDATE ledger SET accepted_by = ?, accepted_at = ? WHERE code_sha = ?", ("owner", time.time(), attested["code_sha"])
        )
        self.set_run("status", "accepted")
        self.store.set_lease(phase="done")
        receipt = self.receipt()
        self.set_run("receipt", receipt)
        self.emit("accepted", code=short(attested["code_sha"]), receipt=receipt)
        await self.say(render_receipt(receipt))
        return receipt

    # Receipt and state ---------------------------------------------------

    def receipt(self) -> dict:
        events = self.store.events(limit=5000)
        kinds = [e["kind"] for e in events]
        failover = self.run("failover") or {}
        fence = next((e for e in events if e["kind"] == "fenced"), None)
        first_after = None
        if fence:
            first_after = next((e for e in events if e["kind"] == "checkpoint" and e["seq"] > fence["seq"]), None)
        started = failover.get("started")
        recovery = round(first_after["at"] - started, 1) if first_after and started else None
        reconciled = next((e for e in events if e["kind"] == "reconciled"), None)
        attested = self.run("attested") or {}
        ledger = self.store.one("SELECT * FROM ledger ORDER BY id DESC LIMIT 1") or {}
        evidence = json.loads(ledger["evidence"]) if ledger else {}
        redone = [e["data"].get("step") for e in events if e["kind"] == "step_redone"]
        return {
            "task": self.task["title"],
            "failure": failover or None,
            "lease": {"from": failover.get("old"), "to": failover.get("new")} if failover else None,
            "checkpoint": (fence or {}).get("data", {}).get("previous"),
            "steps_redone": redone,
            "pr": self.run("pr"),
            "pr_reused": "pr_reused" in kinds or any(i.get("op") == "open_pr" and i.get("status") == "confirmed"
                                                     for i in (reconciled or {}).get("data", {}).get("items", [])),
            "prs_opened": kinds.count("pr_opened"),
            "stale_writes_rejected": kinds.count("stale_write_rejected"),
            "stale_writes_accepted": kinds.count("stale_write_accepted"),
            "inherited": self.run("inherited"),
            "final_commit": short(attested.get("code_sha")),
            "test_commit": short(attested.get("test_sha")),
            "req_version": attested.get("req_version"),
            "verified_by": f"{self.agents['verifier'].name} ({self.agents['verifier'].vendor})",
            "acceptance_tests": len(evidence.get("passed", [])),
            "mutation": evidence.get("mutation"),
            "scrub": [p.get("reason") or p.get("kind") for p in (self.run("scrub") or {}).get("applied", [])],
            "recovery_seconds": recovery,
            "human_messages": self.run("human_messages_during_recovery", 0) if failover else None,
            "authors": json.loads(ledger["authors"]) if ledger else [],
            "accepted_by": ledger.get("accepted_by"),
            "mode": self.mode,
        }

    def state(self) -> dict:
        agents = {row["name"]: row for row in self.store.all("SELECT * FROM agents")}
        now = time.time()
        roster = []
        for info in self.agents.values():
            row = agents.get(info.name, {})
            beat = row.get("last_heartbeat")
            roster.append({
                "name": info.name, "role": info.role, "provider": info.provider, "vendor": info.vendor,
                "state": row.get("state") or "offline", "step": row.get("step"), "note": row.get("note"),
                "alive": bool(beat and now - beat < 6), "last_heartbeat": beat, "last_progress": row.get("last_progress"),
                "pid": row.get("pid"),
            })
        return {
            "task": {"id": self.task["id"], "title": self.task["title"], "request": self.task["request"],
                     "acceptance": self.task["acceptance"], "plan": self.task["plan"]},
            "lease": self.store.lease(),
            "run": {k[4:]: v for k, v in ((r["key"], json.loads(r["value"])) for r in self.store.all("SELECT * FROM kv WHERE key LIKE 'run.%'"))},
            "agents": roster,
            "worklog": self.worklog(),
            "prs": [pr.__dict__ for pr in self.remote.list_prs()],
            "intents": self.store.intents(),
            "now": now,
        }


def render_receipt(r: dict) -> str:
    failure = r.get("failure") or {}
    lines = [f"**Recovery receipt: {r['task']}**", "```"]
    if failure:
        lines.append(f"Failure          {failure.get('failed')} {failure.get('reason')}")
        lines.append(f"Lease            revoked {r['lease']['from']} -> {r['lease']['to']}")
        lines.append(f"Code recovered   checkpoint {r.get('checkpoint') or '-'}; steps redone: {r['steps_redone'] or 'none'}")
        lines.append(f"Pull request     #{r['pr']} {'reused' if r['pr_reused'] else 'opened'}; PRs opened in total: {r['prs_opened']}")
        lines.append(f"Stale writes     {r['stale_writes_rejected']} rejected, {r['stale_writes_accepted']} accepted")
        inherited = r.get("inherited") or {}
        lines.append(f"Inherited work   {len(inherited.get('regressions', []))} regressions, {len(inherited.get('pending', []))} tests pending as expected")
        lines.append(f"Recovery time    {r['recovery_seconds']} s from failure to first new checkpoint")
        lines.append(f"Human messages   {r['human_messages']} during recovery")
    else:
        lines.append("Failure          none; the builder finished on its own")
    mutation = r.get("mutation") or {}
    lines.append(f"Final commit     {r['final_commit']} (tests {r['test_commit']}, {r['req_version']})")
    lines.append(f"Verified by      {r['verified_by']}: {r['acceptance_tests']} acceptance tests, mutation score {mutation.get('score', '-')}%")
    lines.append(f"Scrubbed         {', '.join(r['scrub']) if r['scrub'] else 'nothing'}")
    lines.append(f"Authored by      {', '.join(r['authors'])}; accepted by {r['accepted_by']}")
    lines.append("```")
    return "\n".join(lines)
