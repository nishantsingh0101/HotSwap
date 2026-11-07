"""Subprocess boundaries shared by model execution and verification."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess


def test_environment(workdir: str) -> dict[str, str]:
    # No inherited agent capability, model key, Git credential or Python plugin.
    keep = {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "HOME", "USERPROFILE"}
    return {**{k: v for k, v in os.environ.items() if k in keep},
            "PYTHONPATH": workdir, "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def stop_process_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def stop_child(proc) -> None:
    await asyncio.to_thread(stop_process_tree, proc.pid)
    await proc.wait()
