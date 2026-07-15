"""Filesystem locations for codex and for the swap store."""

from __future__ import annotations

import os
from pathlib import Path


def codex_home() -> Path:
    """The real Codex home directory (respects CODEX_HOME)."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def auth_path(home: Path | None = None) -> Path:
    return (home or codex_home()) / "auth.json"


def swap_home() -> Path:
    """Where codex-swap keeps its own state."""
    env = os.environ.get("CODEX_SWAP_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex-swap"


def store_path() -> Path:
    return swap_home() / "store.json"


def lock_path() -> Path:
    return swap_home() / "lock"


def profiles_dir() -> Path:
    return swap_home() / "profiles"


def ensure_swap_home() -> Path:
    home = swap_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    return home
