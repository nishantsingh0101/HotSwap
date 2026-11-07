import asyncio

import pytest

from hotswap.gateway.core import Refused
from hotswap.gitutil import git


def started(h, run):
    run(h.gw.start_task())
    return h.gw.store.lease()


async def failover(h, failed="builder", reason="stopped responding"):
    h.room.holder = "hs-builder"

    async def release():
        await asyncio.sleep(0.1)
        h.room.holder = None
    asyncio.create_task(release())
    await h.gw.takeover(failed, reason)
    h.room.holder = "hs-standby"
    await h.gw.claim("standby", 2)
    return await h.gw.fence("standby", 2)


def test_push_through_gateway_creates_checkpoint(harness, run):
    started(harness, run)
    work = harness.worktree("builder")
    sha = harness.apply_step(work, 1)
    result = run(harness.gw.push("builder", 1, "b1", sha, str(work), 1))
    assert result["status"] == "done"
    assert harness.remote.branch_head(harness.gw.branch) == sha


def test_direct_push_is_rejected_by_the_remote(harness, run):
    started(harness, run)
    work = harness.worktree("builder")
    sha = harness.apply_step(work, 1)
    direct = git(work, "push", harness.remote.write_url(), f"{sha}:refs/heads/sneaky")
    assert not direct.ok
    assert "Hot Swap gateway" in direct.err


def test_stale_lease_is_rejected_at_the_gateway(harness, run):
    started(harness, run)
    work = harness.worktree("builder")
    run(harness.gw.push("builder", 1, "b1", harness.apply_step(work, 1), str(work), 1))
    run(failover(harness))
    sha = harness.apply_step(work, 2)
    with pytest.raises(Refused) as caught:
        run(harness.gw.push("builder", 1, "late", sha, str(work), 2))
    assert caught.value.code == "stale_lease"


def test_delayed_admitted_push_fails_after_the_fence_commit(harness, run):
    """The old builder's push is admitted before takeover, then lands after it, before the standby pushes."""
    started(harness, run)
    work = harness.worktree("builder")
    run(harness.gw.push("builder", 1, "b1", harness.apply_step(work, 1), str(work), 1))
    late_sha = harness.apply_step(work, 2)

    async def scenario():
        late = asyncio.create_task(harness.gw.push("builder", 1, "b2", late_sha, str(work), 2, delay=6))
        await asyncio.sleep(0.2)
        await failover(harness)
        return await late

    outcome = run(scenario())
    assert outcome["status"] == "rejected"
    head = harness.remote.branch_head(harness.gw.branch)
    assert head == harness.gw.run("fence_sha")
    kinds = [e["kind"] for e in harness.store.events()]
    assert "stale_write_rejected" in kinds
    assert "drained" in kinds


def test_reconcile_finds_existing_pr_and_no_duplicate_is_created(harness, run):
    started(harness, run)
    work = harness.worktree("builder")
    run(harness.gw.push("builder", 1, "b1", harness.apply_step(work, 1), str(work), 1))
    run(harness.gw.open_pr("builder", 1, "b-pr"))
    # the builder dies before reporting the PR in the work log
    run(failover(harness))
    resolved = run(harness.gw.reconcile("standby", 2))
    assert any(item["op"] == "open_pr" for item in resolved) or harness.gw.run("pr") == 1
    again = run(harness.gw.open_pr("standby", 2, "s-pr"))
    assert again["reused"] is True
    assert len(harness.remote.list_prs()) == 1


def test_unknown_push_is_reconciled_against_the_branch(harness, run):
    started(harness, run)
    work = harness.worktree("builder")
    sha = harness.apply_step(work, 1)
    harness.store.add_intent("lost", 1, "builder", "push", {"sha": sha, "step": 1, "expected": None})
    run(failover(harness))
    items = run(harness.gw.reconcile("standby", 2))
    assert {"op": "push", "status": "not_applied"}.items() <= items[0].items()


def test_worklog_overwrite_by_old_builder_is_rejected(harness, run):
    started(harness, run)
    run(failover(harness))
    with pytest.raises(Refused):
        run(harness.gw.append_worklog("builder", 1, "w-late", 3, "step_done", None, "late"))


def test_inherited_check_separates_regressions_from_pending(harness, run):
    started(harness, run)
    harness.gw.record_test_run("checkpoint", "a" * 40, "t", ["t1", "t2"], ["t3", "t4"], [])
    summary = harness.gw.record_test_run("inherited", "b" * 40, "t", ["t1"], ["t2", "t3", "t4"], [])
    assert summary["regressions"] == ["t2"]
    assert summary["pending"] == ["t3", "t4"]


def test_vendor_rule_blocks_attestation(harness, run):
    started(harness, run)
    harness.agents["verifier"].vendor = "vendor-a"
    harness.gw.set_run("head", "c" * 40)
    with pytest.raises(Refused) as caught:
        run(harness.gw.attest("verifier", "c" * 40, "t" * 40, ["x"], [], [], {"score": 90}, "pytest"))
    assert caught.value.code == "same_vendor"


def test_only_the_owner_can_accept_the_exact_verified_commit(harness, run):
    started(harness, run)
    harness.store.put("owner_sender", "@owner")
    head = "d" * 40
    harness.gw.set_run("head", head)
    harness.gw.set_run("test_sha", "t" * 40)
    harness.gw.record_test_run("final", head, "t" * 40, ["x"], [], [])
    run(harness.gw.attest("verifier", head, "t" * 40, ["x"], [], [], {"score": 90}, "pytest"))
    with pytest.raises(Refused) as not_owner:
        run(harness.gw.accept("@teammate", head[:7]))
    assert not_owner.value.code == "not_owner"
    with pytest.raises(Refused) as wrong:
        run(harness.gw.accept("@owner", "abcdef0"))
    assert wrong.value.code == "stale_acceptance"
    receipt = run(harness.gw.accept("@owner", head[:7]))
    assert receipt["accepted_by"] == "owner"


def test_plain_mode_accepts_stale_write_for_comparison(harness, run):
    run(harness.gw.start_task(mode="plain"))
    work = harness.worktree("builder")
    run(harness.gw.push("builder", 1, "b1", harness.apply_step(work, 1), str(work), 1))
    harness.room.holder = None
    run(harness.gw.takeover("builder", "stopped"))
    standby = harness.worktree("standby")
    run(harness.gw.push("standby", 2, "s2", harness.apply_step(standby, 2), str(standby), 2))
    stale = run(harness.gw.push("builder", 1, "b-late", harness.apply_step(work, 3), str(work), 3))
    assert stale["status"] == "done"
    kinds = [e["kind"] for e in harness.store.events()]
    assert "stale_write_accepted" in kinds


def test_late_first_push_fails_when_no_branch_existed_at_takeover(harness, run):
    """Review finding: the old builder's very first push is delayed past a takeover."""
    started(harness, run)
    work = harness.worktree("builder")
    first = harness.apply_step(work, 1)

    async def scenario():
        late = asyncio.create_task(harness.gw.push("builder", 1, "b1", first, str(work), 1, delay=6))
        await asyncio.sleep(0.2)
        await failover(harness)
        return await late

    outcome = run(scenario())
    assert outcome["status"] == "rejected"
    assert harness.remote.branch_head(harness.gw.branch) == harness.gw.run("fence_sha")
    kinds = [e["kind"] for e in harness.store.events()]
    assert "stale_write_accepted" not in kinds
    assert "stale_write_rejected" in kinds


def test_attestation_without_a_recorded_test_run_is_refused(harness, run):
    started(harness, run)
    head = "e" * 40
    harness.gw.set_run("head", head)
    harness.gw.set_run("test_sha", "t" * 40)
    with pytest.raises(Refused) as no_run:
        run(harness.gw.attest("verifier", head, "t" * 40, [], [], [], {}, "pytest"))
    assert no_run.value.code == "no_test_run"
    with pytest.raises(Refused) as wrong_tests:
        run(harness.gw.attest("verifier", head, "nonexistent", ["x"], [], [], {}, "pytest"))
    assert wrong_tests.value.code == "wrong_tests"
    harness.gw.record_test_run("final", head, "t" * 40, ["a", "b"], [], [])
    with pytest.raises(Refused) as mismatch:
        run(harness.gw.attest("verifier", head, "t" * 40, ["a", "b", "c"], [], [], {}, "pytest"))
    assert mismatch.value.code == "evidence_mismatch"


def test_reused_request_id_with_a_different_write_is_refused(harness, run):
    started(harness, run)
    work = harness.worktree("builder")
    sha = harness.apply_step(work, 1)
    run(harness.gw.push("builder", 1, "same-id", sha, str(work), 1))
    other = harness.apply_step(work, 2)
    with pytest.raises(Refused) as caught:
        run(harness.gw.push("builder", 1, "same-id", other, str(work), 2))
    assert caught.value.code == "request_conflict"
