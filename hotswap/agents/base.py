"""Shared runtime for agent processes: a Switch connection, gateway calls and heartbeats."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import httpx

from hotswap.config import Settings, settings as load_settings, state_path
from hotswap.providers import make_provider
from hotswap.switch.admin import load_secrets
from hotswap.switch.client import SwitchAgent


class GatewayRefused(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def parse_command(body: str) -> tuple[str, dict]:
    """'@hs-builder start lease=1 fault=stall' -> ('start', {'lease': '1', 'fault': 'stall'})"""
    words = [w for w in body.replace("**", "").split() if not w.startswith("@")]
    if not words:
        return "", {}
    command = words[0].strip().lower()
    args = {}
    positional = []
    for word in words[1:]:
        if "=" in word:
            key, value = word.split("=", 1)
            args[key] = value
        else:
            positional.append(word)
    if positional:
        args["_"] = positional
    return command, args


class AgentProcess:
    role = "agent"

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or load_settings()
        self.secrets = load_secrets()
        if not all(self.secrets.get("agents", {}).get(role, {}).get("sender") for role in ("gateway", "watchdog")):
            raise RuntimeError("Commander participant IDs are not enrolled. Stop the old gateway and run hotswap setup once.")
        self.role_cfg = self.cfg.role(self.role)
        self.provider = make_provider(self.role_cfg, self.cfg)
        me = self.secrets["agents"][self.role]
        self.switch = SwitchAgent(self.cfg.switch_url, me["id"], me["key"], me["name"], self.secrets["room_id"])
        self.state = "idle"
        self.step: int | None = None
        self.note = ""
        self.error: str | None = None
        self.progress_flag = False
        self.log = logging.getLogger(f"hotswap.{self.role}")
        self.http = None

    # Gateway calls ------------------------------------------------------

    async def call(self, path: str, payload: dict | None = None, method: str = "POST") -> dict:
        headers = {"X-Hotswap-Agent-Token": os.environ.get("HOTSWAP_AGENT_TOKEN", "")}
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=120, verify=False, trust_env=False)
        resp = await self.http.request(method, f"{self.cfg.gateway_url}{path}", json=payload, headers=headers)
        if resp.status_code == 409:
            body = resp.json()
            raise GatewayRefused(body.get("code", "refused"), body.get("reason", ""))
        resp.raise_for_status()
        return resp.json()

    async def run_state(self) -> dict:
        return await self.call("/api/state", method="GET")

    def set(self, state: str, step: int | None = None, note: str = "", progress: bool = False, error: str | None = None) -> None:
        self.state, self.step, self.note = state, step if step is not None else self.step, note
        self.progress_flag = self.progress_flag or progress
        self.error = error

    async def say(self, text: str) -> None:
        try:
            await self.switch.post(text)
        except Exception as error:
            self.log.warning("could not post: %s", error)

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await self.call(f"/api/agents/{self.role}/heartbeat", {
                    "pid": os.getpid(), "state": self.state, "step": self.step, "note": self.note,
                    "progress": self.progress_flag, "error": self.error,
                })
                self.progress_flag = False
            except Exception as error:
                self.log.debug("heartbeat failed: %s", error)
            await asyncio.sleep(1)

    # Messages -----------------------------------------------------------

    async def on_message(self, payload: dict) -> None:
        commanders = {self.secrets["agents"][role].get("sender") for role in ("gateway", "watchdog")}
        commanders.discard(None)
        if len(commanders) != 2:
            raise RuntimeError("Commander participant IDs are not enrolled. Run hotswap setup before starting agents.")
        if payload.get("sender") not in commanders:
            return
        command, args = parse_command(payload.get("body", ""))
        if command:
            # Switch's durable inbox executes commands serially while its socket
            # reader continues to answer heartbeat frames.
            await self._guarded(command, args)

    async def _guarded(self, command: str, args: dict) -> None:
        try:
            await self.handle(command, args)
        except GatewayRefused as refused:
            self.set("refused", note=refused.reason, error=refused.code)
            self.log.warning("gateway refused %s: %s", command, refused)
            raise
        except Exception as error:
            self.set("error", note=str(error)[:200], error=type(error).__name__)
            self.log.exception("%s failed", command)
            raise

    async def handle(self, command: str, args: dict) -> None:
        raise NotImplementedError

    async def startup(self) -> None:
        """Runs once after the Switch connection is open."""

    async def main(self) -> None:
        listener = asyncio.create_task(self.switch.run(self.on_message))
        beats = asyncio.create_task(self.heartbeat_loop())
        await asyncio.wait_for(self.switch.connected.wait(), 60)
        self.set("ready", note=f"{self.provider.label} ready")
        await self.startup()
        await asyncio.gather(listener, beats)


def worktree_path(role: str) -> Path:
    return state_path("work", role, ".keep").parent


def remember(role: str, data: dict) -> None:
    state_path("agents", f"{role}.json").write_text(json.dumps(data))


def recall(role: str) -> dict:
    path = state_path("agents", f"{role}.json")
    return json.loads(path.read_text()) if path.exists() else {}


def crash(reason: str) -> None:
    """Simulates an abrupt failure: no cleanup, no goodbye message, connection just drops."""
    logging.getLogger("hotswap").warning("injected fault: %s", reason)
    sys.stdout.flush()
    os._exit(17)


def run_agent(cls) -> None:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {cls.role} %(levelname)s %(message)s")
    asyncio.run(cls().main())


STEP_COMMIT = re.compile(r"^step (\d+):")
