"""Core account operations: add, switch, remove, alias, enable/disable.

Correctness rules:
- Identity is matched by account_id (stable across token refreshes), never
  by email or slot alone.
- Before any switch, the live auth.json (and every session-profile auth.json)
  is synced back into the store, so a refreshed token is never clobbered by
  restoring a stale copy. OpenAI rotates refresh tokens — restoring an old
  one kills the account until a manual `codex login`.
- Freshness between two copies of the same account is decided by the
  `last_refresh` ISO timestamp inside auth.json.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .auth import (
    AuthError,
    Identity,
    identity_from_auth,
    last_refresh,
    read_auth,
    write_auth_atomic,
)
from .paths import auth_path, codex_home, profiles_dir
from .store import Account, Store, now
from . import oauth
from . import usage as usage_mod
from . import wham


class SwitchError(Exception):
    pass


def _upsert_tokens(store: Store, auth: dict, identity: Identity) -> Optional[Account]:
    """Refresh a stored account's auth from a live copy if it is newer."""
    acc = store.by_account_id(identity.account_id)
    if acc is None:
        return None
    if last_refresh(auth) > last_refresh(acc.auth):
        acc.auth = auth
        acc.updated_at = now()
        # Strictly newer tokens = a fresh login or refresh happened — the
        # session is alive again.
        acc.status = "ok"
    elif last_refresh(auth) == last_refresh(acc.auth):
        acc.auth = auth
    if identity.email:
        acc.email = identity.email
    if identity.plan:
        acc.plan = identity.plan
    return acc


def sync_back(store: Store, home: Optional[Path] = None) -> Optional[Identity]:
    """Pull the newest tokens from auth.json + all session profiles into the store.

    Returns the identity currently active in the main auth.json (None if no
    auth.json exists).
    """
    home = home or codex_home()
    current: Optional[Identity] = None

    auth = read_auth(auth_path(home))
    if auth is not None:
        current = identity_from_auth(auth)
        _upsert_tokens(store, auth, current)
        # A login change made outside cxswap (`codex login`, manual edit) is a
        # switch for attribution purposes: older rollout usage must not be
        # credited to the newly active account.
        if store.last_active_id != current.account_id:
            if store.last_active_id is not None:
                store.last_switch_at = now()
            store.last_active_id = current.account_id

    pdir = profiles_dir()
    if pdir.is_dir():
        for prof in pdir.iterdir():
            pauth_path = prof / "auth.json"
            try:
                pauth = read_auth(pauth_path)
            except AuthError:
                continue
            if pauth is None:
                continue
            try:
                pid = identity_from_auth(pauth)
            except AuthError:
                continue
            _upsert_tokens(store, pauth, pid)

    return current


def snapshot_usage(store: Store, account: Optional[Account], home: Optional[Path] = None) -> None:
    """Attribute the newest rollout rate_limits to `account` (the active one).

    Only snapshots newer than the last switch are trusted — older ones may
    belong to a previously active account.
    """
    if account is None:
        return
    found = usage_mod.latest_rate_limits(home or codex_home(), since=store.last_switch_at)
    if found is None:
        return
    ts, rl = found
    if account.usage_at is None or ts > account.usage_at:
        account.usage = rl
        account.usage_at = ts


def live_refresh_usage(store: Store, account: Optional[Account]) -> bool:
    """Fetch fresh usage for `account` from wham/usage (network).

    Refreshes the access token first if it is about to expire — the rotated
    refresh token is written into the account record, so the caller MUST save
    the store afterwards regardless of the outcome. Returns True when usage
    was updated; on a revoked session marks the account `needs_relogin`.
    Network trouble degrades silently (rollout data stays authoritative).
    """
    if account is None or account.api_key_only or not account.auth:
        return False
    if account.status == "needs_relogin":
        return False

    expires_in = oauth.access_token_expires_in(account.auth)
    if expires_in is not None and expires_in < 300:
        try:
            account.auth = oauth.refresh_tokens(account.auth)
            account.updated_at = now()
        except oauth.RefreshInvalidated:
            account.status = "needs_relogin"
            return False
        except oauth.RefreshError:
            return False

    try:
        raw = wham.fetch_usage(account.auth)
    except wham.WhamError as e:
        if e.code == 401:
            # Access token rejected — try one forced refresh, then retry once.
            try:
                account.auth = oauth.refresh_tokens(account.auth)
                account.updated_at = now()
                raw = wham.fetch_usage(account.auth)
            except oauth.RefreshInvalidated:
                account.status = "needs_relogin"
                return False
            except (oauth.RefreshError, wham.WhamError):
                return False
        else:
            return False

    snap = wham.to_snapshot(raw)
    if snap is None:
        return False
    account.usage = snap
    account.usage_at = now()
    if raw.get("plan_type"):
        account.plan = raw["plan_type"]
    return True


def safe_login(store: Store, home: Optional[Path] = None) -> "Identity":
    """Run `codex login` WITHOUT letting it revoke the current session.

    `codex login` best-effort revokes the refresh token it replaces in
    auth.json (POST /oauth/revoke in openai/codex). Moving auth.json aside
    first means there is nothing to revoke — both sessions stay alive. The
    displaced tokens are already in the store via sync_back.
    """
    import os
    import subprocess

    home = home or codex_home()
    ap = auth_path(home)
    sync_back(store, home)

    codex_bin = shutil.which("codex")
    if codex_bin is None:
        raise SwitchError("codex CLI not found on PATH")

    moved = None
    if ap.exists():
        moved = ap.with_name("auth.json.cxswap-prelogin")
        os.replace(ap, moved)
    try:
        rc = subprocess.call([codex_bin, "login"])
        auth = read_auth(ap) if rc == 0 else None
        if rc != 0 or auth is None:
            raise SwitchError(f"codex login failed (exit {rc})")
    except BaseException:
        # Restore the previous login on any failure or interrupt.
        if moved is not None and not ap.exists():
            os.replace(moved, ap)
            moved = None
        raise
    finally:
        if moved is not None and moved.exists():
            moved.unlink()

    return identity_from_auth(auth)


def add_account(store: Store, alias: Optional[str] = None) -> Account:
    home = codex_home()
    auth = read_auth(auth_path(home))
    if auth is None:
        raise SwitchError(f"no auth.json at {auth_path(home)} — log in with `codex login` first")
    identity = identity_from_auth(auth)

    acc = store.by_account_id(identity.account_id)
    if acc is None:
        acc = Account(
            slot=store.next_slot(),
            account_id=identity.account_id,
            email=identity.email,
            plan=identity.plan,
            api_key_only=identity.api_key_only,
            auth=auth,
            added_at=now(),
            updated_at=now(),
        )
        store.accounts.append(acc)
    else:
        _upsert_tokens(store, auth, identity)
        acc.status = "ok"  # explicit re-add after relogin revives the slot
    if alias:
        acc.alias = alias
    snapshot_usage(store, acc, home)
    return acc


def resolve_target(store: Store, token: str) -> Account:
    """Resolve NUM | alias | email | unique email prefix to an account."""
    token = token.strip()
    if token.isdigit():
        acc = store.by_slot(int(token))
        if acc is None:
            raise SwitchError(f"no account in slot {token}")
        return acc
    for a in store.accounts:
        if a.alias and a.alias.lower() == token.lower():
            return a
    for a in store.accounts:
        if a.email and a.email.lower() == token.lower():
            return a
    prefix = [a for a in store.accounts if a.email and a.email.lower().startswith(token.lower())]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise SwitchError(f"'{token}' is ambiguous: {', '.join(a.email for a in prefix)}")
    raise SwitchError(f"no account matches '{token}'")


def rotation_target(store: Store, current_id: Optional[str]) -> Account:
    """Next enabled account after the current one (by slot order)."""
    candidates = [
        a for a in store.sorted_accounts() if not a.disabled and a.status == "ok"
    ]
    if not candidates:
        raise SwitchError("no enabled accounts in the store — `cxswap add` some first")
    if current_id is None:
        return candidates[0]
    slots = [a.slot for a in candidates]
    cur = store.by_account_id(current_id)
    if cur is None or cur.slot not in slots:
        return candidates[0]
    idx = slots.index(cur.slot)
    nxt = candidates[(idx + 1) % len(candidates)]
    if nxt.account_id == current_id and len(candidates) == 1:
        raise SwitchError("only one enabled account — nothing to rotate to")
    return nxt


def best_target(store: Store, current_id: Optional[str], threshold: float = 100.0) -> Optional[Account]:
    """Account with most remaining quota (lowest worst-window %).

    Unknown or already-reset snapshots count as 0% used. Returns None when no
    candidate is below `threshold`.
    """
    best: Optional[Account] = None
    best_score = None
    for a in store.sorted_accounts():
        if a.disabled or a.api_key_only or a.status != "ok" or a.account_id == current_id:
            continue
        if a.usage and not usage_mod.is_reset(a.usage, a.usage_at):
            score = usage_mod.worst_pct(a.usage) or 0.0
        else:
            score = 0.0
        if best_score is None or score < best_score:
            best, best_score = a, score
    if best is None or (best_score is not None and best_score >= threshold):
        return None
    return best


def do_switch(store: Store, target: Account, home: Optional[Path] = None) -> Identity:
    """Switch the main auth.json to `target`. Store must be saved by caller."""
    home = home or codex_home()
    current = sync_back(store, home)
    if current is not None:
        snapshot_usage(store, store.by_account_id(current.account_id), home)

    fresh = store.by_account_id(target.account_id)
    if fresh is None or not fresh.auth:
        raise SwitchError(f"account {target.display()} has no stored credentials")

    write_auth_atomic(auth_path(home), fresh.auth)
    store.last_switch_at = now()
    store.last_active_id = fresh.account_id
    return identity_from_auth(fresh.auth)


def codex_running() -> bool:
    """Best-effort: is a codex CLI process alive right now?"""
    if shutil.which("pgrep") is None:
        return False
    try:
        r = subprocess.run(
            ["pgrep", "-f", r"(^|/)codex( |$)"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False
