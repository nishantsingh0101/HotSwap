import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest

from hotswap.gateway.core import Refused
from hotswap.providers import OpenAICompatibleProvider
from hotswap.switch.journal import Journal, DeliveryBlocked


def test_takeover_during_head_lookup_cannot_replace_the_fence(harness, run):
    h = harness
    run(h.gw.start_task())
    work = h.worktree("builder")
    sha = h.apply_step(work, 1)
    original = h.remote.branch_head
    started, release = threading.Event(), threading.Event()
    first = [True]

    def delayed(branch):
        if first[0]:
            first[0] = False
            started.set()
            if not release.wait(20):
                raise RuntimeError("review test synchronization timeout")
        return original(branch)

    h.remote.branch_head = delayed

    async def scenario():
        late = asyncio.create_task(h.gw.push("builder", 1, "lookup-race", sha, str(work), 1))
        while not started.is_set():
            await asyncio.sleep(.01)
        try:
            await h.gw.takeover("builder", "lookup delayed")
            h.room.holder = "hs-standby"
            await h.gw.claim("standby", 2)
            fence = await h.gw.fence("standby", 2)
        finally:
            release.set()
        with pytest.raises(Refused, match="stale lease"):
            await late
        assert original(h.gw.branch) == fence["sha"]
        assert not h.store.intent("lookup-race")

    run(scenario())


@pytest.mark.parametrize("name", ["tests/acceptance/../../../escape.txt", "/tmp/escape.py", "C:/escape.py", "tests\\acceptance\\..\\escape.py"])
def test_model_cannot_escape_allowed_directory(tmp_path, name):
    provider = object.__new__(OpenAICompatibleProvider)
    with pytest.raises(ValueError):
        provider._write(tmp_path, [{"path": name, "content": "x"}], allowed_prefix="tests/acceptance/")
    assert list(tmp_path.iterdir()) == []


def test_model_cannot_follow_symlink_outside_allowed_directory(tmp_path):
    work, outside = tmp_path / "work", tmp_path / "outside"
    (work / "tests/acceptance").mkdir(parents=True)
    outside.mkdir()
    try:
        (work / "tests/acceptance/link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    provider = object.__new__(OpenAICompatibleProvider)
    with pytest.raises(ValueError):
        provider._write(work, [{"path": "tests/acceptance/link/no.py", "content": "x"}], allowed_prefix="tests/acceptance/")
    assert list(outside.iterdir()) == []


def test_journal_deduplicates_and_preserves_uncertain_commands(tmp_path):
    path = tmp_path / "delivery.sqlite"
    j = Journal(path)
    connection = j.get("connection")
    j.enqueue("room:msg1", {"body": "start"}, 10)
    j.enqueue("room:msg1", {"body": "start"}, 11)
    assert j.next()[0] == "room:msg1"
    j.db.close()
    j = Journal(path)
    assert j.get("connection") == connection
    assert j.get("cursor") == 11
    with pytest.raises(DeliveryBlocked):
        j.next()
    j.abandon_for_recovery()
    assert j.next() is None
    j.enqueue("room:msg2", {"body": "takeover"}, 12)
    key, _ = j.next()
    j.done(key)
    j.enqueue("room:msg2", {}, 13)
    assert j.next() is None
    j.db.close()


def test_restart_revokes_authority_without_erasing_receipts(harness, run):
    h = harness
    run(h.gw.start_task())
    h.store.set_lease(phase="fencing")
    h.store.add_intent("uncertain", 1, "builder", "open_pr", {"head": h.gw.branch})
    assert h.gw.recover_after_restart()
    assert h.gw.run("status") == "blocked"
    assert h.store.lease()["number"] == 2
    assert h.store.intent("uncertain")["status"] == "unknown"
    assert h.gw.run("base_sha")
    assert not h.gw.recover_after_restart()


def test_reset_cannot_reuse_authority_or_event_sequence(harness, run):
    run(harness.gw.start_task())
    old = harness.store.lease()["number"]
    sequence = harness.store.events()[-1]["seq"]
    harness.store.reset_run()
    run(harness.gw.start_task())
    assert harness.store.lease()["number"] > old
    assert harness.store.events()[0]["seq"] > sequence


def test_uncertain_switch_command_blocks_writes(harness, run):
    async def unavailable(*args):
        raise RuntimeError("connection lost during send")
    harness.room.targeted = unavailable
    with pytest.raises(Refused, match="Switch command delivery failed"):
        run(harness.gw.start_task())
    assert harness.gw.run("status") == "blocked"
    assert harness.gw.run("interrupted")
    assert harness.store.lease()["holder"] is None


def test_role_lookup_failure_is_not_treated_as_a_free_role(harness, run):
    run(harness.gw.start_task())
    async def unavailable():
        raise RuntimeError("Switch offline")
    harness.room.role_holder = unavailable
    harness.gw.role_release_seconds = .01
    run(harness.gw.takeover("builder", "lost connection"))
    assert harness.gw.run("status") == "blocked"
    assert not any(names == ["hs-standby"] for names, _ in harness.room.targets)


def test_operator_controls_require_separate_capability(harness, tmp_path, monkeypatch, run):
    import hotswap.config as cfg
    isolated = tmp_path / "isolated-app"
    isolated.mkdir()
    monkeypatch.setattr(cfg, "STATE_DIR", isolated)
    import hotswap.gateway.app as api
    # Module initialization may open an isolated store; never touch the real app DB.
    original = api.GW
    monkeypatch.setattr(api, "GW", harness.gw)
    monkeypatch.setattr(api, "operator_token", lambda: "test-operator")
    async def start(mode="hotswap", fault=None):
        return await harness.gw.start_task(mode, fault)
    monkeypatch.setattr(api, "start", start)

    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as c:
            for path in ("/api/demo/start", "/api/demo/reset", "/api/demo/kill/builder", "/api/demo/revive/builder"):
                r = await c.post(path, json={"mode": "plain"})
                assert r.status_code == 401
            assert not harness.gw.run("status")
            r = await c.post("/api/demo/start", json={"mode": "plain"}, headers={"X-Hotswap-Agent-Token": api.AGENT_TOKENS["builder"]})
            assert r.status_code == 401
            r = await c.post("/api/demo/start", json={"mode": "hotswap"}, headers={"X-Hotswap-Operator-Token": "test-operator", "Origin": "https://elsewhere.invalid"})
            assert r.status_code == 403
            r = await c.post("/api/demo/start", json={"mode": "hotswap"}, headers={"X-Hotswap-Operator-Token": "test-operator"})
            assert r.status_code == 200
    try:
        run(requests())
    finally:
        if original is not harness.gw:
            original.store.db.close()

def test_empty_acceptance_manifest_cannot_start_implementation(harness):
    with pytest.raises(Refused, match='acceptance-test files'):
        harness.gw.record_tests_pushed('verifier', 'a' * 40, 0)
    assert harness.gw.run('test_sha') is None


def test_cli_provider_rejects_success_without_written_tests(tmp_path, run):
    from hotswap.providers import ClaudeCodeProvider
    provider = object.__new__(ClaudeCodeProvider)
    async def no_files(*args, **kwargs):
        return 'I described tests but did not write them.'
    provider._run = no_files
    with pytest.raises(RuntimeError, match='no acceptance-test files'):
        run(provider.write_tests(tmp_path, {'request': 'CSV export', 'acceptance': []}))

def test_cli_completion_does_not_wait_for_descendant_pipe_eof(tmp_path, run):
    import sys
    import time
    from hotswap.providers import CliProvider
    class PythonProbe(CliProvider):
        command = [sys.executable, '-c']
    provider = object.__new__(PythonProbe)
    prompt = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)']); print('READY')"
    before = time.monotonic()
    assert 'READY' in run(provider._run(tmp_path, prompt, timeout=2))
    assert time.monotonic() - before < 2
