"""The gateway service: HTTP API for agents, live stream for the console, Switch room bridge,
the watchdog loop and the agent supervisor."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets as pysecrets
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from hotswap.config import EXAMPLE_DIR, ROLES, ROOT, SCENARIO_DIR, STATE_DIR, settings, state_path
from hotswap.gateway.core import AgentInfo, Gateway, NullRoom, Refused, short
from hotswap.gateway.remote import GitHubRemote, LocalRemote
from hotswap.gateway.store import Store
from hotswap.switch.admin import load_secrets
from hotswap.switch.client import SwitchAgent
from hotswap.operator import operator_token
from hotswap.runtime import stop_process_tree

log = logging.getLogger("hotswap.gateway")
CFG = settings()
TASK = json.loads((SCENARIO_DIR / "ledgerlite" / "task.json").read_text())
CONSOLE_DIR = Path(__file__).resolve().parent.parent / "console"


class SwitchRoom:
    """The gateway's and watchdog's presence in the Switch room."""

    def __init__(self, gateway: SwitchAgent, watchdog: SwitchAgent, store: Store) -> None:
        self.gateway = gateway
        self.watchdog = watchdog
        self.store = store

    async def post(self, text: str, as_role: str = "gateway") -> None:
        agent = self.watchdog if as_role == "watchdog" else self.gateway
        await agent.post(text)

    async def targeted(self, names: list[str], text: str) -> None:
        await self.gateway.targeted(names, text)

    async def role_holder(self) -> str | None:
        return await self.gateway.role_holder("builder")

    async def write_doc(self, title: str, content: str) -> None:
        doc_id = self.store.get("doc_id")
        if doc_id:
            try:
                await self.gateway.op("update_room_document", {"document_id": doc_id, "content": content})
                return
            except RuntimeError:
                pass
        result = await self.gateway.op("create_room_document", {
            "name": title, "description": "Plan, checkpoints and pull request for the current task. Written only by the gateway.",
            "instructions": "Read this to see which steps are done, by whom, and at which commit. Only the gateway edits it.",
            "content": content,
        })
        self.store.put("doc_id", (result or {}).get("id") or (result or {}).get("document_id"))


# Each agent process gets its own secret at spawn time. Every agent call must carry it,
# so one agent cannot act as another, and nothing outside the supervisor can act as an agent.
AGENT_TOKENS = {role: pysecrets.token_hex(24) for role in ROLES}


def require_agent(request: Request, name: str) -> None:
    token = request.headers.get("X-Hotswap-Agent-Token", "")
    expected = AGENT_TOKENS.get(name)
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail=f"not authenticated as {name}")


class Supervisor:
    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self.spawned_at: dict[str, float] = {}

    def spawn(self, role: str, zombie: bool = False) -> None:
        old = self.procs.get(role)
        if old and old.poll() is None:
            return
        logs = state_path("logs", f"{role}.log")
        env = dict(os.environ, PYTHONUNBUFFERED="1", HOTSWAP_AGENT_TOKEN=AGENT_TOKENS[role])
        if zombie:
            env["HOTSWAP_ZOMBIE"] = "1"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.spawned_at[role] = time.time()
        self.procs[role] = subprocess.Popen(
            [sys.executable, "-m", "hotswap.agents.run", role], cwd=ROOT, env=env,
            stdout=open(logs, "a", encoding="utf-8"), stderr=subprocess.STDOUT, creationflags=flags,
        )

    def alive(self, role: str) -> bool | None:
        proc = self.procs.get(role)
        return None if proc is None else proc.poll() is None

    def stop(self, role: str) -> None:
        proc = self.procs.get(role)
        if proc and proc.poll() is None:
            stop_process_tree(proc.pid)

    def stop_all(self) -> None:
        for role in list(self.procs):
            self.stop(role)

    async def wait_ready(self, roles, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = True
            for role in roles:
                proc = self.procs.get(role)
                row = GW.store.one("SELECT pid,state,last_heartbeat FROM agents WHERE name=?", (role,))
                if not proc or proc.poll() is not None:
                    raise Refused("agent_unavailable", f"{role} did not start. Check its log and Switch enrollment.")
                # A Windows venv launcher and the Python worker have different
                # PIDs. Its per-role capability and a post-spawn heartbeat bind
                # this readiness report to the newly supervised worker.
                if not row or (row["last_heartbeat"] or 0) < self.spawned_at.get(role, 0) or row["state"] not in ("ready", "idle") or time.time() - (row["last_heartbeat"] or 0) > 6:
                    ready = False
            if ready:
                return
            await asyncio.sleep(.2)
        raise Refused("agents_not_ready", "Agents did not connect in time. Check logs before retrying.")


def build_gateway(room=None) -> Gateway:
    STATE_DIR.mkdir(exist_ok=True)
    store = Store(state_path("state.db"))
    if CFG.remote_kind == "github":
        remote = GitHubRemote(CFG.github_repo, CFG.github_token)
    else:
        remote = LocalRemote(state_path("remote.git"), store)
    agents = {}
    enrolled = load_secrets().get("agents", {})
    for role in ROLES:
        rc = CFG.role(role)
        agents[role] = AgentInfo(role, role, enrolled.get(role, {}).get("name", f"hs-{role}"), rc.provider, rc.vendor)
    if agents["scrubber"].provider == "replay":
        agents["scrubber"].vendor = "code checks"
    return Gateway(store, remote, room or NullRoom(), TASK, agents, state_path("gateway-clone", ".keep").parent,
                   drain_seconds=CFG.drain_seconds, faults_enabled=CFG.faults_enabled)


GW: Gateway = build_gateway()
SUP = Supervisor()


def ensure_remote() -> None:
    if isinstance(GW.remote, LocalRemote) and not (GW.remote.path / "HEAD").exists():
        GW.remote.create(EXAMPLE_DIR, os.urandom(16).hex())


# Watchdog -----------------------------------------------------------------

async def watchdog_loop() -> None:
    while True:
        await asyncio.sleep(1)
        try:
            lease = GW.store.lease()
            holder = lease["holder"]
            if lease["phase"] != "active" or holder not in ("builder", "standby"):
                continue
            row = GW.store.one("SELECT * FROM agents WHERE name = ?", (holder,))
            if not row or GW.run("status") not in ("building", "recovering"):
                continue
            now = time.time()
            reason = None
            silent = now - (row["last_heartbeat"] or now)
            if SUP.alive(holder) is False:
                # a dead process is a failure whatever state it last reported
                reason = "stopped: its process exited and its Switch connection dropped"
            elif row["state"] in ("done", "ready", "idle", "claiming"):
                continue
            elif silent > max(CFG.dead_seconds * 3, 20):
                reason = f"stopped responding (no heartbeat for {silent:.0f} s)"
            elif row["state"] == "working" and row["last_progress"] and now - row["last_progress"] > CFG.stall_seconds:
                budget = CFG.stall_seconds if GW.agents[holder].provider == "replay" else max(CFG.stall_seconds, 960)
                if now - row["last_progress"] > budget:
                    reason = f"is still connected but made no progress for {budget:.0f} s"
            else:
                errors = json.loads(row["errors"] or "[]")
                if len(errors) >= 3 and len(set(errors[-3:])) == 1 and row["last_progress"] and now - row["last_progress"] > 8:
                    reason = f"is looping on the same error: {errors[-1]}"
            if reason:
                GW.emit("watchdog_alert", agent=holder, reason=reason)
                asyncio.create_task(GW.takeover(holder, reason))
        except Exception:
            log.exception("watchdog tick failed")


# Switch listener ----------------------------------------------------------

async def on_room_message(payload: dict) -> None:
    sender = payload.get("sender")
    body = payload.get("body") or ""
    owner, mate = GW.store.get("owner_sender"), GW.store.get("teammate_sender")
    if sender not in (owner, mate):
        return
    GW.emit("human_message", who="owner" if sender == owner else "teammate", body=body[:200])
    try:
        await GW.handle_human_message(sender, body)
    except Refused:
        return
    lowered = body.lower()
    if payload.get("addressed") and "where are we" in lowered:
        await GW.say(status_text())
    elif payload.get("addressed") and sender == owner and re.search(r"\bstart\b", lowered):
        fault = re.search(r"fault=(\w+)", body)
        try:
            await start(mode="hotswap", fault=fault.group(1) if fault else None)
        except Refused as error:
            await GW.say(error.reason)


def status_text() -> str:
    state = GW.state()
    lease, run = state["lease"], state["run"]
    done = sorted({e["step"] for e in state["worklog"] if e["status"] == "step_done"})
    plan = state["task"]["plan"]
    failover = run.get("failover")
    lines = [
        f"**Where we are:** {run.get('status', 'idle').replace('_', ' ')}.",
        f"Steps done: {', '.join(map(str, done)) or 'none'} of {len(plan)}. Lease {lease['number']} is held by {lease['holder'] or 'nobody'}.",
        f"Branch head `{short(run.get('head'))}`, PR #{run.get('pr') or '-'}.",
    ]
    if failover:
        lines.append(f"Earlier: `{failover['failed']}` {failover['reason']}; the standby took over as lease {failover['new']}.")
    return "\n".join(lines)


async def start(mode: str = "hotswap", fault: str | None = None) -> dict:
    if GW.run("status") not in (None, "accepted", "rejected"):
        raise Refused("task_in_progress", "A task or recovery is already open. Recover or explicitly reset it first.")
    ensure_remote()
    for role in ROLES:
        SUP.spawn(role)
    await SUP.wait_ready(ROLES)
    state = await GW.start_task(mode=mode, fault=fault)
    GW.set_run("start_args", {"fault": fault})
    return state


# App ------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_remote()
    interrupted = GW.recover_after_restart()
    tasks = [asyncio.create_task(watchdog_loop())]
    secrets = load_secrets()
    if secrets.get("room_id"):
        a = secrets["agents"]
        gw_agent = SwitchAgent(CFG.switch_url, a["gateway"]["id"], a["gateway"]["key"], a["gateway"]["name"], secrets["room_id"])
        wd_agent = SwitchAgent(CFG.switch_url, a["watchdog"]["id"], a["watchdog"]["key"], a["watchdog"]["name"], secrets["room_id"])
        GW.room = SwitchRoom(gw_agent, wd_agent, GW.store)
        GW.store.put("owner_sender", secrets.get("owner_sender"))
        GW.store.put("teammate_sender", secrets.get("teammate_sender"))
        tasks.append(asyncio.create_task(gw_agent.run(on_room_message, addressed_only=False)))
        tasks.append(asyncio.create_task(wd_agent.run(lambda _p: asyncio.sleep(0))))
        if not interrupted and os.environ.get("HOTSWAP_NO_AGENTS") != "1" and not GW.run("interrupted"):
            for role in ROLES:
                SUP.spawn(role)
    else:
        log.warning("Switch is not set up yet; run `hotswap setup`. The console runs without a room.")
    yield
    for task in tasks:
        task.cancel()
    SUP.stop_all()


app = FastAPI(title="Hot Swap gateway", lifespan=lifespan)


@app.middleware("http")
async def protect_operator_controls(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/demo/") or path == "/api/run-demo":
        supplied = request.headers.get("X-Hotswap-Operator-Token", "")
        if not hmac.compare_digest(supplied, operator_token()):
            return JSONResponse({"reason": "Open the operator link printed by hotswap run to use demo controls."}, status_code=401)
        if path.startswith("/api/demo/") and not CFG.faults_enabled:
            return JSONResponse({"reason": "Demo controls are disabled (HOTSWAP_FAULTS=0)."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"reason": "Cross-origin operator requests are refused."}, status_code=403)
    return await call_next(request)


@app.exception_handler(Refused)
async def refused_handler(_request: Request, exc: Refused) -> JSONResponse:
    return JSONResponse({"code": exc.code, "reason": exc.reason}, status_code=409)


def full_state() -> dict:
    state = GW.state()
    state["room_name"] = CFG.room_name
    state["read_url"] = GW.remote.read_url()
    state["remote_kind"] = GW.remote.kind
    state["connected"] = isinstance(GW.room, SwitchRoom) and GW.room.gateway.connected.is_set()
    state["supervised"] = {role: SUP.alive(role) for role in ROLES}
    state["matrix"] = load_matrix()
    return state


@app.get("/api/state")
async def api_state() -> dict:
    return full_state()


@app.get("/api/stream")
async def api_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue()
    GW.listeners.append(queue)
    after = int(request.query_params.get("after", 0))

    async def events():
        try:
            for event in GW.store.events(after=after):
                yield {"event": "hs", "data": json.dumps(event)}
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15)
                    yield {"event": "hs", "data": json.dumps(event)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            GW.listeners.remove(queue)

    return EventSourceResponse(events())


@app.get("/api/events")
async def api_events(after: int = 0) -> list[dict]:
    return GW.store.events(after=after)


@app.get("/api/matrix")
async def api_matrix() -> dict | None:
    return load_matrix()


@app.post("/api/agents/{name}/heartbeat")
async def api_heartbeat(name: str, body: dict, request: Request) -> dict:
    require_agent(request, name)
    return GW.heartbeat(name, body["pid"], body["state"], body.get("step"), body.get("note", ""),
                        body.get("progress", False), body.get("error"))


@app.post("/api/ops/push")
async def api_push(body: dict, request: Request) -> dict:
    require_agent(request, body["agent"])
    task = asyncio.ensure_future(GW.push(body["agent"], body["lease"], body["request_id"], body["sha"], body["source"],
                                         body.get("step"), float(body.get("delay", 0)), body.get("branch")))
    return await asyncio.shield(task)


@app.post("/api/ops/open_pr")
async def api_open_pr(body: dict, request: Request) -> dict:
    require_agent(request, body["agent"])
    return await GW.open_pr(body["agent"], body["lease"], body["request_id"], body.get("branch"))


@app.post("/api/ops/worklog")
async def api_worklog(body: dict, request: Request) -> dict:
    require_agent(request, body["agent"])
    return await GW.append_worklog(body["agent"], body["lease"], body["request_id"], body.get("step"),
                                   body["status"], body.get("commit"), body.get("note", ""))


@app.post("/api/ops/done")
async def api_done(body: dict, request: Request) -> dict:
    require_agent(request, body["agent"])
    await GW.builder_done(body["agent"], body["lease"])
    return {"status": "ok"}


@app.post("/api/takeover/claim")
async def api_claim(body: dict, request: Request) -> dict:
    require_agent(request, body["agent"])
    return await GW.claim(body["agent"], body["lease"])


@app.post("/api/ops/fence")
async def api_fence(body: dict, request: Request) -> dict:
    require_agent(request, body["agent"])
    return await GW.fence(body["agent"], body["lease"])


@app.post("/api/ops/reconcile")
async def api_reconcile(body: dict, request: Request) -> dict:
    require_agent(request, body["agent"])
    return {"items": await GW.reconcile(body["agent"], body["lease"])}


@app.post("/api/ops/tests_push")
async def api_tests_push(body: dict, request: Request) -> dict:
    require_agent(request, "verifier")
    branch = f"hotswap/tests/{TASK['id']}"
    expected = GW.remote.branch_head(branch)
    result = await GW.remote.push(Path(body["source"]), body["sha"], branch, expected)
    if not result.ok:
        raise Refused("tests_push_failed", result.err)
    GW.record_tests_pushed(body["agent"], body["sha"], body.get("count", 0))
    fault = (GW.run("start_args") or {}).get("fault") or GW.run("fault")
    await GW.tell("builder", f"start lease={GW.store.lease()['number']}" + (f" fault={fault}" if fault else ""))
    return {"status": "done"}


@app.post("/api/verify/run")
async def api_verify_run(body: dict, request: Request) -> dict:
    require_agent(request, "verifier")
    return GW.record_test_run(body["kind"], body["code_sha"], body.get("test_sha"), body["passed"],
                              body["failed"], body.get("repo_failed", []))


@app.post("/api/verify/attest")
async def api_attest(body: dict, request: Request) -> dict:
    require_agent(request, "verifier")
    return await GW.attest(body["agent"], body["code_sha"], body["test_sha"], body["passed"], body["failed"],
                           body.get("repo_failed", []), body.get("mutation", {}), body.get("command", ""))


@app.get("/api/scrub/context")
async def api_scrub_context(request: Request) -> dict:
    require_agent(request, "scrubber")
    return {
        "read_url": GW.remote.read_url(), "base": GW.run("base_sha"), "head": GW.run("head"),
        "added": await asyncio.to_thread(GW.added_lines), "plan_files": sorted({f for s in TASK["plan"] for f in s["files"]}),
        "public_names": TASK["public_names"],
    }


@app.post("/api/ops/scrub")
async def api_scrub(body: dict, request: Request) -> dict:
    require_agent(request, "scrubber")
    return await GW.apply_scrub(body.get("proposals", []))


@app.post("/api/events/{name}")
async def api_agent_event(name: str, body: dict, request: Request) -> dict:
    require_agent(request, name)
    kind = body.pop("kind")
    if kind == "escalate":
        await GW.escalate(body.get("reason", "the agent asked for help"))
    else:
        GW.emit(kind, agent=name, **body)
    return {"status": "ok"}


# Demo controls used by the console ----------------------------------------

@app.post("/api/demo/start")
async def api_demo_start(body: dict) -> dict:
    await start(mode=body.get("mode", "hotswap"), fault=body.get("fault") or None)
    return full_state()


@app.post("/api/demo/kill/{role}")
async def api_demo_kill(role: str) -> dict:
    GW.emit("manual_kill", agent=role)
    GW.kill_agent(role) or SUP.stop(role)
    return {"status": "stopped"}


@app.post("/api/demo/revive/{role}")
async def api_demo_revive(role: str, body: dict | None = None) -> dict:
    SUP.spawn(role, zombie=bool((body or {}).get("zombie")))
    GW.emit("agent_revived", agent=role, zombie=bool((body or {}).get("zombie")))
    return {"status": "started"}


@app.post("/api/demo/reset")
async def api_demo_reset() -> dict:
    if any(GW.inflight.values()):
        raise Refused("writes_in_flight", "Wait for admitted writes to finish before resetting this run.")
    SUP.stop_all()
    await asyncio.sleep(1)
    GW.store.reset_run()
    if isinstance(GW.remote, LocalRemote):
        GW.remote.create(EXAMPLE_DIR, os.urandom(16).hex())
    for path in (STATE_DIR / "work", STATE_DIR / "gateway-clone", STATE_DIR / "sandbox", STATE_DIR / "agents"):
        _remove_tree(path)
    for role in ROLES:
        SUP.spawn(role)
    GW.emit("reset")
    return full_state()


@app.post("/api/demo/recover")
async def api_demo_recover() -> dict:
    """Explicit recovery of a preserved interrupted run, never a silent replay."""
    interrupted = GW.run("interrupted")
    if not interrupted or GW.run("status") != "blocked":
        raise Refused("not_interrupted", "There is no interrupted gateway run to recover.")
    if not isinstance(GW.room, SwitchRoom) or not GW.room.gateway.connected.is_set():
        raise Refused("switch_offline", "Reconnect the Switch room before recovering work.")
    from hotswap.switch.journal import Journal
    for path in (STATE_DIR / "streams").glob("*.sqlite"):
        journal = Journal(path)
        journal.abandon_for_recovery()
        journal.db.close()
    previous = interrupted["lease"]
    holder = previous.get("holder") or "builder"
    # Previously issued worker capabilities were revoked on restart. Fresh
    # workers consume NEW recovery commands; old commands are never retried.
    for role in ROLES:
        if role != "builder":
            SUP.spawn(role)
    await SUP.wait_ready([role for role in ROLES if role != "builder"])
    if interrupted["status"] in ("scrubbing", "verifying", "awaiting_acceptance"):
        GW.store.set_lease(holder=holder, phase="verifying")
        await GW.request_final_verification()
    else:
        GW.set_run("inherited", None)
        GW.store.set_lease(holder="builder", phase="active")
        await GW.takeover("builder", "gateway restarted; reconciling the preserved run")
    GW.set_run("interrupted", None)
    return full_state()


@app.get("/api/run-demo")
async def api_run_demo() -> dict:
    """Runs the finished feature, so the console can show it working."""
    head = GW.run("head")
    if not head:
        return {"ok": False, "output": "nothing built yet"}
    from hotswap.verify import Sandbox, run_isolated
    if TASK["demo_command"][0] != "python":
        raise Refused("unsupported_demo", "Only an isolated Python demo command is supported")
    box = await asyncio.to_thread(Sandbox, GW.remote.read_url())
    try:
        await asyncio.to_thread(box.checkout, head, None)
        proc = await asyncio.to_thread(run_isolated, box.path, TASK["demo_command"][1:], 60)
        return {"ok": proc.returncode == 0, "command": " ".join(TASK["demo_command"]), "output": proc.stdout + proc.stderr}
    finally:
        await asyncio.to_thread(box.remove)


def load_matrix() -> dict | None:
    path = STATE_DIR / "matrix.json"
    return json.loads(path.read_text()) if path.exists() else None


def _remove_tree(path: Path) -> None:
    import shutil
    import stat

    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.exists():
        shutil.rmtree(path, onerror=onerror)


if CONSOLE_DIR.exists():
    app.mount("/assets", StaticFiles(directory=CONSOLE_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    async def console(path: str):
        root = CONSOLE_DIR.resolve()
        target = (root / path).resolve()
        if path and target.is_relative_to(root) and target.is_file():
            return FileResponse(target)
        return FileResponse(CONSOLE_DIR / "index.html")
