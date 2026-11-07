"""hotswap: command line for setting up and running Hot Swap."""
from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys

import httpx
import typer

from hotswap.config import DEPLOY_DIR, ROOT, STATE_DIR, settings

app = typer.Typer(add_completion=False, help="Recovery for agent teams in Switch rooms.")
switch_app = typer.Typer(help="Run the local Switch server (Docker).")
app.add_typer(switch_app, name="switch")

COMPOSE = ["docker", "compose", "-p", "hotswap", "-f", str(DEPLOY_DIR / "docker-compose.yml"),
           "--env-file", str(DEPLOY_DIR / ".env"), "--profile", "collab", "--profile", "gateway"]
SWITCH_VERSION = "0.31.0"


def _set(lines: list[str], key: str, value: str) -> list[str]:
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            return lines
    return lines + [f"{key}={value}"]


@app.command()
def init() -> None:
    """Create deploy/.env (Switch secrets) and .env (Hot Swap settings) if they are missing."""
    target = DEPLOY_DIR / ".env"
    if not target.exists():
        lines = (DEPLOY_DIR / "switch.env.example").read_text().splitlines()
        for key in ("DB_PASSWORD", "DB_OWNER_PASSWORD", "AGENT_REGISTRATION_TOKEN", "GATEWAY_ADMIN_PASSWORD",
                    "MATTERMOST_ADMIN_PASSWORD", "MATTERMOST_USER_PASSWORD"):
            lines = _set(lines, key, secrets.token_hex(24))
        for key, value in {
            "SECRET_KEYS": f"hotswap:{secrets.token_hex(32)}", "CONTROLLER_TOKEN_SECRET": secrets.token_hex(32),
            "JWT_SECRET_KEY": secrets.token_hex(32), "SWITCH_VERSION": SWITCH_VERSION,
            "API_HOST_PORT": "18000", "GATEWAY_HOST_PORT": "13000", "MATTERMOST_HOST_PORT": "18065",
            "POSTGRES_HOST_PORT": "15432", "FEATURE_FLAGS_ENABLED": "agent_management",
        }.items():
            lines = _set(lines, key, value)
        target.write_text("\n".join(lines) + "\n")
        typer.echo("created deploy/.env with fresh secrets")
    if not (ROOT / ".env").exists():
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
        typer.echo("created .env from .env.example")
    typer.echo("ready. Next: hotswap switch up")


@switch_app.command("up")
def switch_up() -> None:
    """Start Switch, Mattermost and Postgres (pinned to Switch 0.31.0)."""
    subprocess.run([*COMPOSE, "up", "-d"], check=True)
    typer.echo("Switch API http://127.0.0.1:18000   Mattermost http://127.0.0.1:18065")


@switch_app.command("down")
def switch_down(wipe: bool = typer.Option(False, help="Also delete Switch data volumes.")) -> None:
    subprocess.run([*COMPOSE, "down", *(["-v"] if wipe else [])], check=True)


@app.command()
def verifier_image() -> None:
    """Build the network-isolated verification runtime (once)."""
    subprocess.run(["docker", "build", "-t", "hotswap-verifier:0.1", str(DEPLOY_DIR / "verifier")], check=True)


@app.command()
def setup() -> None:
    """Create the room, agents, builder role and enroll both people by their Switch identity."""
    from hotswap.switch.admin import setup as do_setup
    do_setup(settings(), log=typer.echo)
    typer.echo("setup complete. Next: hotswap run")


@app.command()
def login() -> None:
    """Show the Mattermost logins for the owner and the teammate."""
    from hotswap.switch.admin import deploy_env, load_secrets
    env, data = deploy_env(), load_secrets()
    typer.echo(f"Mattermost: {settings().mattermost_url}")
    typer.echo(f"  owner     user / {env.get('MATTERMOST_USER_PASSWORD')}")
    if data.get("teammate"):
        typer.echo(f"  teammate  {data['teammate']['username']} / {data['teammate']['password']}")


@app.command()
def run(no_agents: bool = typer.Option(False, help="Start the gateway without spawning agents.")) -> None:
    """Start the gateway, watchdog, console and the agent processes."""
    import os
    import uvicorn
    if no_agents:
        os.environ["HOTSWAP_NO_AGENTS"] = "1"
    cfg = settings()
    from hotswap.operator import operator_token
    typer.echo(f"operator console: {cfg.gateway_url}/#operator={operator_token()}")
    uvicorn.run("hotswap.gateway.app:app", host=cfg.gateway_host, port=cfg.gateway_port, log_level="warning")


@app.command()
def demo(fault: str = typer.Option("after_pr_before_report", help="Where the builder fails."),
         mode: str = typer.Option("hotswap", help="hotswap, plain or fresh.")) -> None:
    """Start the task with an injected failure."""
    from hotswap.operator import operator_token
    resp = httpx.post(f"{settings().gateway_url}/api/demo/start", json={"fault": fault, "mode": mode}, timeout=60,
                      headers={"X-Hotswap-Operator-Token": operator_token()})
    resp.raise_for_status()
    typer.echo(f"started in {mode} mode, builder will fail at: {fault}")


@app.command()
def status() -> None:
    """Print the current lease, step progress and pull request."""
    state = httpx.get(f"{settings().gateway_url}/api/state", timeout=30).json()
    lease, run_state = state["lease"], state["run"]
    typer.echo(f"status {run_state.get('status', 'idle')} | lease {lease['number']} {lease['holder']} ({lease['phase']}) | PR #{run_state.get('pr')}")
    for agent in state["agents"]:
        typer.echo(f"  {agent['name']:<9} {agent['state']:<10} {agent['provider']} ({agent['vendor']})  {agent['note'] or ''}")


@app.command()
def matrix(no_comparison: bool = typer.Option(False, help="Skip the plain-Switch and fresh-agent comparison.")) -> None:
    """Run the crash matrix against the running gateway and save the results."""
    from hotswap.matrix import run_matrix
    results = run_matrix(include_comparison=not no_comparison, log=typer.echo)
    typer.echo(json.dumps(results, indent=2))


@app.command()
def recover() -> None:
    """Reconcile an interrupted gateway run using new worker commands."""
    from hotswap.operator import operator_token
    resp = httpx.post(f"{settings().gateway_url}/api/demo/recover", timeout=120,
                      headers={"X-Hotswap-Operator-Token": operator_token()})
    resp.raise_for_status()
    typer.echo("Recovery requested. Follow the preserved run in the Switch room.")


@app.command()
def reset() -> None:
    """Reset the task, the example repository and agent workspaces."""
    from hotswap.operator import operator_token
    httpx.post(f"{settings().gateway_url}/api/demo/reset", timeout=120,
               headers={"X-Hotswap-Operator-Token": operator_token()}).raise_for_status()
    typer.echo("reset")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
