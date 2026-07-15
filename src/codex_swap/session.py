"""Session mode: run codex as a specific account in this terminal only.

A per-account profile dir under ~/.codex-swap/profiles/<slot>/ becomes
CODEX_HOME for the launched process. Everything in the real ~/.codex is
symlinked into the profile (config.toml, prompts, sessions, sqlite, caches…)
so behaviour and history stay shared — except auth.json, which is a real
file holding that account's credentials. Other terminals and the default
login are untouched.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from .auth import identity_from_auth, read_auth, write_auth_atomic
from .paths import codex_home, profiles_dir
from .store import Account
from .switcher import SwitchError

# Never share these into a profile: auth is per-profile, and swap-internal
# files don't exist in ~/.codex anyway.
_EXCLUDE = {"auth.json"}


def profile_path(account: Account) -> Path:
    return profiles_dir() / f"slot-{account.slot}"


def _sync_symlinks(profile: Path, real: Path) -> None:
    # mkdir's mode does not apply to parents — tighten them explicitly, the
    # profiles hold per-account auth.json copies.
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    for p in (profile, profile.parent):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    for entry in real.iterdir():
        if entry.name in _EXCLUDE:
            continue
        link = profile / entry.name
        if link.is_symlink():
            if link.resolve(strict=False) == entry.resolve(strict=False):
                continue
            link.unlink()
        elif link.exists():
            # Profile accumulated a real file (codex created it before the
            # shared one existed) — leave it alone rather than destroy data.
            continue
        link.symlink_to(entry)


def prepare_profile(account: Account) -> Path:
    real = codex_home()
    if not real.is_dir():
        raise SwitchError(f"codex home {real} does not exist")
    profile = profile_path(account)
    _sync_symlinks(profile, real)

    # Freshest tokens win: if the profile already refreshed its copy, keep it.
    existing = None
    try:
        existing = read_auth(profile / "auth.json")
    except Exception:  # noqa: BLE001
        existing = None
    if existing is not None:
        try:
            eid = identity_from_auth(existing)
            if (
                eid.account_id == account.account_id
                and (existing.get("last_refresh") or "") >= (account.auth.get("last_refresh") or "")
            ):
                return profile
        except Exception:  # noqa: BLE001
            pass
    write_auth_atomic(profile / "auth.json", account.auth)
    return profile


def run_codex(account: Account, extra_args: Optional[List[str]] = None) -> int:
    """Exec codex with CODEX_HOME pointed at the account's profile."""
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        raise SwitchError("codex CLI not found on PATH")
    profile = prepare_profile(account)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(profile)
    argv = [codex_bin, *(extra_args or [])]
    if os.name == "posix":
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(codex_bin, argv, env)
        raise AssertionError("unreachable")
    import subprocess

    return subprocess.call(argv, env=env)
