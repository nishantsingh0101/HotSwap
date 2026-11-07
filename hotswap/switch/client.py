"""A small Switch agent client: WebSocket events in, HTTP operations out.

Frame format (Switch 0.31): server frames are {"event", "data", "id"}; the client
answers every ping with {"type": "pong", "cursor": n}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from pathlib import Path
from hotswap.config import state_path
from hotswap.switch.journal import Journal, DeliveryBlocked

import httpx
import websockets

log = logging.getLogger("hotswap.switch")

MessageHandler = Callable[[dict], Awaitable[None]]


@dataclass
class SwitchAgent:
    base_url: str
    agent_id: str
    api_key: str
    name: str
    room_id: str
    connection_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cursor: int = 0
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    journal_path: Path | None = None

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "X-Switch-Connection-Id": self.connection_id}

    async def op(self, operation: str, args: dict | None = None, timeout: float = 60) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(
                f"{self.base_url}/agents/{self.agent_id}/ops/{operation}", headers=self.headers, json=args or {}
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"{operation} failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json().get("result")

    async def post(self, body: str) -> None:
        if self.connected.is_set():
            await self.op("post_message", {"body": body})
        else:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(f"{self.base_url}/agents/{self.agent_id}/message",
                    headers={"Authorization": f"Bearer {self.api_key}"}, json={"room_id": self.room_id, "content": body})
                response.raise_for_status()

    async def targeted(self, names: list[str], body: str) -> None:
        await self.op("send_targeted_message", {"body": body, "target_names": names})

    async def assume_role(self, role: str) -> dict:
        return await self.op("assume_role", {"role": role})

    async def release_role(self) -> dict:
        return await self.op("release_role", {})

    async def role_holder(self, role: str) -> str | None:
        detail = await self.op("get_role_detail", {"room_id": self.room_id, "role_name": role})
        return _holder_name(detail)

    async def run(self, on_message: MessageHandler, addressed_only: bool = True) -> None:
        """Read frames independently of serial durable command execution."""
        journal = Journal(self.journal_path or state_path("streams", f"{self.agent_id}.sqlite"))
        self.connection_id = journal.get("connection")
        self.cursor = journal.get("cursor", 0)
        if journal.get("blocked"):
            journal.db.close()
            raise DeliveryBlocked("Switch journal is blocked; operator recovery required")
        async def consume():
            while True:
                item = journal.next()
                if item:
                    key, payload = item
                    await on_message(payload)
                    journal.done(key)
                else:
                    await asyncio.sleep(0.1)
        worker = asyncio.create_task(consume())
        reader = asyncio.create_task(self._receive(journal, addressed_only))
        try:
            done, _ = await asyncio.wait((worker, reader), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.connected.clear()
            worker.cancel(); reader.cancel()
            await asyncio.gather(worker, reader, return_exceptions=True)
            journal.db.close()

    async def _receive(self, journal: Journal, addressed_only: bool) -> None:
        backoff = 1.0
        generation = journal.get("generation")
        while True:
            params = f"connection_id={self.connection_id}&rooms={self.room_id}"
            if self.cursor:
                params += f"&start_from={self.cursor}"
            if generation is not None:
                params += f"&expected_generation={generation}"
            url = self.base_url.replace("http", "ws", 1) + f"/agents/{self.agent_id}/connection/ws?{params}"
            try:
                async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {self.api_key}"},
                                              ping_interval=None, open_timeout=20) as sock:
                    backoff = 1.0
                    async for raw in sock:
                        frame = json.loads(raw)
                        kind, data = frame.get("event"), frame.get("data") or {}
                        if kind == "ping":
                            await sock.send(json.dumps({"type": "pong", "cursor": self.cursor}))
                        elif kind == "connection_state":
                            if not self.cursor:
                                self.cursor = data.get("cursor") or 0
                            generation = data.get("generation")
                            journal.put("generation", generation)
                            self.connected.set()
                        elif kind == "gap":
                            journal.put("blocked", "event stream gap")
                            raise DeliveryBlocked("Switch event gap; context must be reconciled before continuing")
                        elif kind == "refused":
                            log.error("connection refused: %s", data)
                            await asyncio.sleep(3)
                            break
                        elif kind == "evicted":
                            if (data.get("code") or data.get("reason")) == "taken_over":
                                journal.put("blocked", "connection taken over")
                                raise DeliveryBlocked("Switch connection taken over; standing down")
                            break
                        elif kind == "message":
                            payload = data.get("payload") or {}
                            if payload.get("addressed") or not addressed_only:
                                message_id = payload.get("message_id")
                                if not message_id:
                                    journal.put("blocked", "message has no stable ID")
                                    raise DeliveryBlocked("Message has no stable ID")
                                journal.enqueue(f"{self.room_id}:{message_id}", payload, frame.get("id") or self.cursor)
                            self.cursor = max(self.cursor, frame.get("id") or 0)
                            journal.put("cursor", self.cursor)
                        elif frame.get("id"):
                            self.cursor = max(self.cursor, frame["id"])
                            journal.put("cursor", self.cursor)
            except asyncio.CancelledError:
                raise
            except DeliveryBlocked:
                raise
            except Exception as error:
                log.warning("switch connection lost: %s", error)
            self.connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)


def _holder_name(detail: dict | None) -> str | None:
    """get_role_detail returns live holders in `held_by` as [{name, present_here, session_room}]."""
    if not detail:
        return None
    held_by = detail.get("held_by")
    if isinstance(held_by, list):
        return (held_by[0].get("name") if isinstance(held_by[0], dict) else str(held_by[0])) if held_by else None
    for key in ("holder", "current_holder", "holder_name"):
        value = detail.get(key)
        if isinstance(value, dict):
            return value.get("name") or value.get("agent_name")
        if isinstance(value, str) and value:
            return value
    holders = detail.get("holders") or detail.get("live_holders")
    if isinstance(holders, list) and holders:
        first = holders[0]
        return first.get("name") or first.get("agent_name") if isinstance(first, dict) else str(first)
    return None
