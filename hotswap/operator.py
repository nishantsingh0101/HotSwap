"""Local operator capability. Never include it in an agent's environment."""
from __future__ import annotations

import os
import secrets

from hotswap.config import state_path


def operator_token() -> str:
    path = state_path("operator-token")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    value = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
    return value
