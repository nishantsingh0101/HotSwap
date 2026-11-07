"""The crash matrix: kill the builder at each risky point and record what really happens."""
from __future__ import annotations

import json
import time

import httpx

from hotswap.config import STATE_DIR, settings

SCENARIOS = [
    ("crash_before_first_commit", "Before the first commit", "hotswap", False),
    ("crash_after_commit", "After a commit, before the push", "hotswap", False),
    ("after_pr_before_report", "After opening the PR, before reporting it", "hotswap", False),
    ("crash_during_tests", "During the test run", "hotswap", False),
    ("stall", "Builder stalls while still connected", "hotswap", False),
    ("loop", "Builder loops on the same error", "hotswap", False),
    ("delayed_push", "Old push admitted, then delayed past the takeover", "hotswap", False),
    ("after_pr_before_report", "Old builder comes back and pushes", "hotswap", True),
]
COMPARISON = [
    ("fresh", "No shared context, protections off"),
    ("plain", "Switch room, Hot Swap protections off"),
    ("hotswap", "Switch + Hot Swap"),
]
FINISHED = {"awaiting_acceptance", "accepted", "rejected", "blocked", "escalated"}


def run_one(http: httpx.Client, fault: str, mode: str, zombie: bool, timeout: float = 420) -> dict:
    http.post("/api/demo/reset").raise_for_status()
    time.sleep(8)
    started = time.time()
    http.post("/api/demo/start", json={"mode": mode, "fault": fault}).raise_for_status()
    revived = False
    state = {}
    while time.time() - started < timeout:
        time.sleep(2)
        state = http.get("/api/state").json()
        run = state["run"]
        if zombie and not revived and (run.get("fence_sha") or (mode != "hotswap" and run.get("failover"))):
            time.sleep(3 if mode == "hotswap" else 8)
            http.post("/api/demo/revive/builder", json={"zombie": True})
            revived = True
        if run.get("status") in FINISHED:
            break
    events = [e["kind"] for e in _events(http)]
    run = state.get("run", {})
    inherited = run.get("inherited") or {}
    failover = run.get("failover") or {}
    fence_seq = None
    return {
        "fault": fault, "mode": mode, "zombie": zombie,
        "status": run.get("status"),
        "recovered": run.get("status") in ("awaiting_acceptance", "accepted"),
        "failover_reason": failover.get("reason"),
        "prs_opened": events.count("pr_opened"),
        "duplicate_prs": max(0, events.count("pr_opened") - 1),
        "pr_reused": "pr_reused" in events or any(i.get("op") == "open_pr" and (i.get("result") or {}).get("reconciled") == "confirmed" for i in state.get("intents", [])),
        "stale_writes_rejected": events.count("stale_write_rejected"),
        "stale_writes_accepted": events.count("stale_write_accepted"),
        "steps_redone": events.count("step_redone"),
        "steps_adopted": events.count("step_adopted"),
        "regressions": len(inherited.get("regressions", [])),
        "tests_passed": next((e for e in reversed(_events(http)) if e["kind"] == "attested"), {}).get("data", {}).get("passed"),
        "mutation": next((e for e in reversed(_events(http)) if e["kind"] == "attested"), {}).get("data", {}).get("mutation", {}).get("score"),
        "seconds": round(time.time() - started, 1),
    }


def _events(http: httpx.Client) -> list[dict]:
    out, after = [], 0
    while True:
        # the stream endpoint replays stored events; the state API carries the run, so read the DB-backed list
        resp = http.get("/api/events", params={"after": after})
        batch = resp.json()
        if not batch:
            return out
        out += batch
        after = batch[-1]["seq"]


def run_matrix(include_comparison: bool = True, log=print) -> dict:
    cfg = settings()
    from hotswap.operator import operator_token
    http = httpx.Client(base_url=cfg.gateway_url, timeout=60,
                        headers={"X-Hotswap-Operator-Token": operator_token()})
    results = {"started": time.time(), "crash_points": [], "comparison": []}
    for fault, label, mode, zombie in SCENARIOS:
        log(f"running: {label}")
        outcome = run_one(http, fault, mode, zombie)
        outcome["label"] = label
        results["crash_points"].append(outcome)
        save(results)
        log(f"  -> {outcome['status']} in {outcome['seconds']} s")
    if include_comparison:
        for mode, label in COMPARISON:
            log(f"comparison: {label}")
            outcome = run_one(http, "after_pr_before_report", mode, True)
            outcome["label"] = label
            results["comparison"].append(outcome)
            save(results)
            log(f"  -> {outcome['status']} in {outcome['seconds']} s")
    results["finished"] = time.time()
    save(results)
    return results


def save(results: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "matrix.json").write_text(json.dumps(results, indent=2))
