"""Account store: load/save with locking, atomic writes and a .bak copy."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator, Optional

from .paths import ensure_swap_home, lock_path, store_path

STORE_VERSION = 1

DEFAULT_SETTINGS = {
    "autoswitch.threshold": 90,
    "autoswitch.cooldown": 300,
    "autoswitch.interval": 60,
}


@dataclass
class Account:
    slot: int
    account_id: str
    email: Optional[str] = None
    plan: Optional[str] = None
    alias: Optional[str] = None
    disabled: bool = False
    # "ok" | "needs_relogin" (session revoked server-side — only a fresh
    # `codex login` + `cxswap add` revives it)
    status: str = "ok"
    api_key_only: bool = False
    auth: dict = field(default_factory=dict)
    added_at: float = 0.0
    updated_at: float = 0.0
    usage: Optional[dict] = None  # last-known rate_limits snapshot
    usage_at: Optional[float] = None

    @property
    def label(self) -> str:
        return self.email or (self.alias or f"account-{self.slot}")

    def display(self) -> str:
        parts = [self.label]
        if self.alias and self.alias != self.label:
            parts.append(f"({self.alias})")
        return " ".join(parts)


@dataclass
class Store:
    version: int = STORE_VERSION
    accounts: list = field(default_factory=list)  # list[Account]
    settings: dict = field(default_factory=dict)
    last_switch_at: float = 0.0
    last_active_id: Optional[str] = None

    def setting(self, key: str) -> Any:
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def by_account_id(self, account_id: str) -> Optional[Account]:
        for a in self.accounts:
            if a.account_id == account_id:
                return a
        return None

    def by_slot(self, slot: int) -> Optional[Account]:
        for a in self.accounts:
            if a.slot == slot:
                return a
        return None

    def next_slot(self) -> int:
        return max((a.slot for a in self.accounts), default=0) + 1

    def sorted_accounts(self) -> list:
        return sorted(self.accounts, key=lambda a: a.slot)


class StoreError(Exception):
    pass


@contextmanager
def locked() -> Iterator[None]:
    """Serialize cxswap invocations (POSIX flock)."""
    ensure_swap_home()
    lp = lock_path()
    with open(lp, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_store() -> Store:
    sp = store_path()
    try:
        with open(sp, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return Store()
    except json.JSONDecodeError as e:
        raise StoreError(
            f"{sp} is corrupt ({e}). A backup may exist at {sp}.bak"
        ) from e

    accounts = [Account(**a) for a in raw.get("accounts", [])]
    return Store(
        version=raw.get("version", STORE_VERSION),
        accounts=accounts,
        settings=raw.get("settings", {}),
        last_switch_at=raw.get("last_switch_at", 0.0),
        last_active_id=raw.get("last_active_id"),
    )


def save_store(store: Store) -> None:
    ensure_swap_home()
    sp = store_path()
    if sp.exists():
        try:
            shutil.copy2(sp, str(sp) + ".bak")
        except OSError:
            pass
    payload = {
        "version": store.version,
        "accounts": [asdict(a) for a in store.accounts],
        "settings": store.settings,
        "last_switch_at": store.last_switch_at,
        "last_active_id": store.last_active_id,
    }
    fd, tmp = tempfile.mkstemp(dir=str(sp.parent), prefix=".store-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, sp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def now() -> float:
    return time.time()
