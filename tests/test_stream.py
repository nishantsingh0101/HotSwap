import asyncio
import json

import pytest
import websockets

from hotswap.switch.client import SwitchAgent
from hotswap.switch.journal import DeliveryBlocked, Journal
from hotswap.agents.base import AgentProcess


def test_socket_heartbeats_continue_during_serial_deduplicated_work(tmp_path):
    async def scenario():
        handled = []
        completed = asyncio.Event()
        pongs = []

        async def handle(payload):
            await asyncio.sleep(.25)
            handled.append(payload["message_id"])
            completed.set()

        async def server(sock):
            await sock.send(json.dumps({"event": "connection_state", "data": {"cursor": 0, "generation": 1}}))
            payload = {"addressed": True, "message_id": "stable-1", "body": "work", "sender": "opaque"}
            for sequence in (1, 2):
                await sock.send(json.dumps({"event": "message", "id": sequence, "data": {"payload": payload}}))
            for _ in range(3):
                await sock.send(json.dumps({"event": "ping", "data": {}}))
                response = json.loads(await asyncio.wait_for(sock.recv(), 1))
                pongs.append(response["type"])
                await asyncio.sleep(.05)
            await asyncio.wait_for(completed.wait(), 2)
            await sock.send(json.dumps({"event": "gap", "data": {"reason": "test"}}))
            await sock.wait_closed()

        async with websockets.serve(server, "127.0.0.1", 0) as server_instance:
            port = server_instance.sockets[0].getsockname()[1]
            client = SwitchAgent(f"http://127.0.0.1:{port}", "fake-agent", "fake-key", "fake", "room", journal_path=tmp_path / "stream.sqlite")
            with pytest.raises(DeliveryBlocked, match="gap"):
                await asyncio.wait_for(client.run(handle), 5)
            assert handled == ["stable-1"]
            assert pongs == ["pong"] * 3
            assert not client.connected.is_set()
        journal = Journal(tmp_path / "stream.sqlite")
        assert journal.get("blocked")
        assert journal.get("cursor") == 2
        journal.db.close()
    asyncio.run(scenario())


def test_commander_display_name_does_not_authorize_a_command():
    async def scenario():
        agent = object.__new__(AgentProcess)
        agent.secrets = {"agents": {"gateway": {"sender": "opaque-gateway"}, "watchdog": {"sender": "opaque-watchdog"}}}
        seen = []
        async def handle(command, args):
            seen.append(command)
        agent._guarded = handle
        await agent.on_message({"sender_name": "hs-gateway", "sender": "somebody-else", "body": "start"})
        assert seen == []
        await agent.on_message({"sender_name": "different-name", "sender": "opaque-gateway", "body": "start"})
        assert seen == ["start"]
    asyncio.run(scenario())
