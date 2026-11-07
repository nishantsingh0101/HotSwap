"""One-time setup against a local Switch server: users, agents, room, role and identities."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from pathlib import Path

import httpx
import websockets
from dotenv import dotenv_values

from hotswap.config import DEPLOY_DIR, ROLES, Settings, state_path

AGENTS = {
    "gateway": ("hs-gateway", "Gateway", "Owns the work log, leases and the recovery receipt."),
    "watchdog": ("hs-watchdog", "Watchdog", "Watches progress and starts failover."),
    "builder": ("hs-builder", "Builder", "Builds the requested feature, one checkpoint per step."),
    "standby": ("hs-standby", "Standby", "Takes over the build when the builder fails."),
    "verifier": ("hs-verifier", "Verifier", "Writes acceptance tests first and verifies the final commit."),
    "scrubber": ("hs-scrubber", "Scrubber", "Proposes removal of leftover debris before final verification."),
}
PROFILE = {
    "connection_model": "always_on",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
}
TEAMMATE = ("sam", "Sam Iyer")


def secrets_path() -> Path:
    return state_path("switch.json")


def load_secrets() -> dict:
    path = secrets_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_secrets(data: dict) -> None:
    secrets_path().write_text(json.dumps(data, indent=2))


def deploy_env() -> dict:
    return dotenv_values(DEPLOY_DIR / ".env")


def check(resp: httpx.Response, *ok: int) -> httpx.Response:
    if resp.status_code not in (ok or (200, 201)):
        raise RuntimeError(f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text[:300]}")
    return resp


def gateway_session(settings: Settings, email: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=f"{settings.switch_url}/gateway", timeout=60)
    check(client.post("/auth/login", json={"email": email, "password": password}))
    return client


def mattermost_session(settings: Settings, login: str, password: str) -> httpx.Client:
    resp = check(httpx.post(f"{settings.mattermost_url}/api/v4/users/login",
                            json={"login_id": login, "password": password}, timeout=60))
    return httpx.Client(base_url=f"{settings.mattermost_url}/api/v4", timeout=60,
                        headers={"Authorization": f"Bearer {resp.headers['Token']}"})


def wait_for(url: str, seconds: int = 240) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError(f"{url} did not become ready in {seconds} s")


def setup(settings: Settings, log=print) -> dict:
    env = deploy_env()
    data = load_secrets()
    wait_for(f"{settings.switch_url}/health")
    wait_for(f"{settings.mattermost_url}/api/v4/system/ping")
    admin = gateway_session(settings, env["GATEWAY_ADMIN_EMAIL"], env["GATEWAY_ADMIN_PASSWORD"])

    mm_admin = mattermost_session(settings, env["MATTERMOST_ADMIN_USER"], env["MATTERMOST_ADMIN_PASSWORD"])
    team = check(mm_admin.get(f"/teams/name/{env['MATTERMOST_TEAM_NAME']}")).json()
    bridges = check(admin.get("/collaborations")).json()
    bridge_id = next(b["bridge_id"] for b in bridges if b["bridge_type"] == "mattermost")
    data["bridge_id"] = bridge_id

    # Teammate: a second person who can watch the room and ask questions, but cannot accept work.
    username, full_name = TEAMMATE
    teammate = data.setdefault("teammate", {"username": username, "password": secrets.token_urlsafe(14) + "aA1!"})
    resp = mm_admin.get(f"/users/username/{username}")
    if resp.status_code == 200:
        teammate["mm_id"] = resp.json()["id"]
    else:
        first, last = full_name.split(" ", 1)
        teammate["mm_id"] = check(mm_admin.post("/users", json={
            "email": f"{username}@hotswap.example", "username": username, "password": teammate["password"],
            "first_name": first, "last_name": last,
        })).json()["id"]
    check(mm_admin.post(f"/teams/{team['id']}/members", json={"team_id": team["id"], "user_id": teammate["mm_id"]}), 200, 201)
    log("people ready: owner 'user', teammate 'sam'")

    # Agents, all owned by the workspace owner.
    if "registration_key" not in data:
        data["registration_key"] = check(admin.post("/api-keys", json={"label": "hotswap"})).json()["key"]
    agents = data.setdefault("agents", {})
    for role, (name, display, description) in AGENTS.items():
        if role in agents:
            continue
        reg = check(httpx.post(f"{settings.switch_url}/agents", timeout=60,
                               headers={"Authorization": f"Bearer {data['registration_key']}"},
                               json={"name": name, "display_name": display, "description": description,
                                     "connector_type": "hotswap", "integration_profile": PROFILE}))
        agents[role] = {"name": name, "id": reg.json()["id"], "key": reg.json()["api_key"]}
        save_secrets(data)
    log(f"agents registered: {', '.join(a['name'] for a in agents.values())}")

    # The gateway answers anyone in the room (people ask it "where are we?").
    check(admin.put(f"/agents/{agents['gateway']['id']}/addressing-policy", json={
        "policy": {"rules": [{"users": "*", "agents": "*", "owner": True, "owner_agents": True}]}}))

    gw = agents["gateway"]
    gw_headers = {"Authorization": f"Bearer {gw['key']}"}
    if "room_id" not in data:
        result = check(httpx.post(f"{settings.switch_url}/agents/{gw['id']}/ops/create_room", timeout=90, headers=gw_headers, json={
            "name": settings.room_name,
            "description": "Hot Swap: recovery for agent teams.",
            "agent_names": [a["name"] for a in agents.values()],
            "user_names": ["user", username],
            "instructions": ROOM_INSTRUCTIONS,
        })).json()["result"]
        data["room_id"] = result.get("room_id") or result["id"]
        save_secrets(data)
    log(f"room ready: {settings.room_name}")

    owner_sender, teammate_sender = asyncio.run(_room_setup(settings, data, env, team["id"]))
    data["owner_sender"] = owner_sender
    data["teammate_sender"] = teammate_sender
    save_secrets(data)
    log("builder role defined and both people enrolled by their Switch identity")
    return data


ROOM_INSTRUCTIONS = (
    "This room runs Hot Swap. The builder role is exclusive. Every write goes through the gateway. "
    "Agents act only on messages addressed to them. Only the owner accepts finished work."
)


async def _room_setup(settings: Settings, data: dict, env: dict, team_id: str) -> tuple[str, str]:
    gw = data["agents"]["gateway"]
    connection_id = str(uuid.uuid4())
    url = settings.switch_url.replace("http", "ws", 1) + f"/agents/{gw['id']}/connection/ws?connection_id={connection_id}&rooms={data['room_id']}"
    headers = {"Authorization": f"Bearer {gw['key']}", "X-Switch-Connection-Id": connection_id}
    nonce = secrets.token_hex(4)
    agent_nonces = {role: secrets.token_hex(24) for role in ("gateway", "watchdog")}
    senders: dict[str, str] = {}

    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {gw['key']}"}, ping_interval=None) as sock:
        ready = asyncio.Event()

        async def reader(stream=sock, ready_event=ready) -> None:
            async for raw in stream:
                frame = json.loads(raw)
                kind, payload = frame.get("event"), (frame.get("data") or {}).get("payload") or {}
                if kind == "ping":
                    await stream.send(json.dumps({"type": "pong", "cursor": 0}))
                elif kind == "connection_state":
                    ready_event.set()
                elif kind == "message":
                    body = payload.get("body")
                    for who in ("owner", "teammate"):
                        if body == f"enroll {who} {nonce}":
                            senders.setdefault(who, payload["sender"])
                    for role, proof in agent_nonces.items():
                        if body == f"enroll-agent {role} {proof}":
                            data["agents"][role].setdefault("sender", payload["sender"])

        task = asyncio.create_task(reader())
        await asyncio.wait_for(ready.wait(), 30)
        async with httpx.AsyncClient(timeout=60) as http:
            roles = (await http.post(f"{settings.switch_url}/agents/{gw['id']}/ops/list_roles", headers=headers, json={})).json()
            names = [r.get("name") for r in (roles.get("result") or [])] if isinstance(roles, dict) else []
            if "builder" not in names:
                check(await http.post(f"{settings.switch_url}/agents/{gw['id']}/ops/define_role", headers=headers, json={
                    "name": "builder", "exclusive": True,
                    "instructions": "You hold the builder lease. Write only through the gateway and checkpoint every step.",
                }))

        owner_mm = mattermost_session(settings, env["MATTERMOST_USER"], env["MATTERMOST_USER_PASSWORD"])
        mate_mm = mattermost_session(settings, data["teammate"]["username"], data["teammate"]["password"])
        channel = check(owner_mm.get(f"/teams/{team_id}/channels/name/{settings.room_name}")).json()
        data["channel_id"] = channel["id"]
        mate_mm.post(f"/channels/{channel['id']}/members", json={"user_id": data["teammate"]["mm_id"]})
        check(owner_mm.post("/posts", json={"channel_id": channel["id"], "message": f"enroll owner {nonce}"}))
        check(mate_mm.post("/posts", json={"channel_id": channel["id"], "message": f"enroll teammate {nonce}"}))
        # Observe each commander using the OTHER authenticated agent connection.
        # Participant IDs are opaque and are not the registration agent IDs.
        wd = data["agents"]["watchdog"]
        wd_url = settings.switch_url.replace("http", "ws", 1) + f"/agents/{wd['id']}/connection/ws?connection_id={uuid.uuid4()}&rooms={data['room_id']}"
        async with websockets.connect(wd_url, additional_headers={"Authorization": f"Bearer {wd['key']}"}, ping_interval=None) as wd_sock:
            wd_ready = asyncio.Event()
            wd_task = asyncio.create_task(reader(wd_sock, wd_ready))
            try:
                await asyncio.wait_for(wd_ready.wait(), 30)
                async with httpx.AsyncClient(timeout=30) as http:
                    for role, proof in agent_nonces.items():
                        agent = data["agents"][role]
                        check(await http.post(f"{settings.switch_url}/agents/{agent['id']}/message",
                            headers={"Authorization": f"Bearer {agent['key']}"},
                            json={"room_id": data["room_id"], "content": f"enroll-agent {role} {proof}"}))
                deadline = time.time() + 30
                while time.time() < deadline:
                    if len(senders) == 2 and all(data["agents"][r].get("sender") for r in agent_nonces):
                        break
                    await asyncio.sleep(0.5)
            finally:
                wd_task.cancel()
                await asyncio.gather(wd_task, return_exceptions=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if len(senders) < 2:
        raise RuntimeError("could not enroll both people; check that the room is bridged to Mattermost")
    if not all(data["agents"][r].get("sender") for r in agent_nonces):
        raise RuntimeError("Could not enroll commander participant IDs; refusing name-based authorization")
    return senders["owner"], senders["teammate"]
