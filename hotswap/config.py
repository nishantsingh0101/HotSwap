"""Settings and paths. Values come from HotSwap/.env, with safe defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

STATE_DIR = Path(os.environ.get("HOTSWAP_STATE_DIR", str(ROOT / ".hotswap"))).resolve()
DEPLOY_DIR = ROOT / "deploy"
SCENARIO_DIR = Path(__file__).resolve().parent / "scenario"
EXAMPLE_DIR = ROOT / "example"

ROLES = ("builder", "standby", "verifier", "scrubber")

# Default vendor label for each provider kind. The vendor rule compares labels.
PROVIDER_VENDOR = {
    "claude-code": "anthropic",
    "codex": "openai",
    "opencode": "opencode",
    "openai": "openai-compatible",
    "replay": "replay",
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class RoleConfig:
    role: str
    provider: str
    vendor: str
    model: str


@dataclass(frozen=True)
class Settings:
    switch_url: str = field(default_factory=lambda: env("HOTSWAP_SWITCH_URL", "http://127.0.0.1:18000"))
    mattermost_url: str = field(default_factory=lambda: env("HOTSWAP_MATTERMOST_URL", "http://127.0.0.1:18065"))
    gateway_host: str = field(default_factory=lambda: env("HOTSWAP_GATEWAY_HOST", "127.0.0.1"))
    gateway_port: int = field(default_factory=lambda: int(env("HOTSWAP_GATEWAY_PORT", "7600")))
    room_name: str = field(default_factory=lambda: env("HOTSWAP_ROOM", "hotswap-demo"))
    remote_kind: str = field(default_factory=lambda: env("HOTSWAP_REMOTE", "local"))
    github_repo: str = field(default_factory=lambda: env("HOTSWAP_GITHUB_REPO"))
    github_token: str = field(default_factory=lambda: env("GITHUB_TOKEN"))
    stall_seconds: float = field(default_factory=lambda: float(env("HOTSWAP_STALL_SECONDS", "25")))
    dead_seconds: float = field(default_factory=lambda: float(env("HOTSWAP_DEAD_SECONDS", "6")))
    drain_seconds: float = field(default_factory=lambda: float(env("HOTSWAP_DRAIN_SECONDS", "5")))
    faults_enabled: bool = field(default_factory=lambda: env("HOTSWAP_FAULTS", "1") == "1")
    openai_base_url: str = field(default_factory=lambda: env("HOTSWAP_OPENAI_BASE_URL", "https://api.openai.com/v1"))
    openai_api_key: str = field(default_factory=lambda: env("HOTSWAP_OPENAI_API_KEY"))

    @property
    def gateway_url(self) -> str:
        return f"http://{self.gateway_host}:{self.gateway_port}"

    def role(self, role: str) -> RoleConfig:
        key = role.upper()
        provider = env(f"HOTSWAP_{key}_PROVIDER", "replay")
        vendor = env(f"HOTSWAP_{key}_VENDOR") or PROVIDER_VENDOR.get(provider, provider)
        if provider == "replay" and not env(f"HOTSWAP_{key}_VENDOR"):
            # Recorded runs still need distinct labels so the vendor rule can be exercised.
            vendor = {"builder": "replay-a", "standby": "replay-b", "verifier": "replay-c"}.get(role, "replay")
        model = env(f"HOTSWAP_{key}_MODEL")
        return RoleConfig(role=role, provider=provider, vendor=vendor, model=model)


def settings() -> Settings:
    return Settings()


def state_path(*parts: str) -> Path:
    path = STATE_DIR.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
