"""Automatic switching: watch rollout usage, rotate before the limit hits.

Usage data comes from local rollout files, which codex updates on every turn —
so while codex is actively working (the only time limits matter) the numbers
are live. When codex is idle no new data appears and auto simply holds.

Exit codes for --once (cron-friendly, mirrors claude-swap):
  0 switched · 1 error · 2 nothing to do · 3 blocked (no viable target)
"""

from __future__ import annotations

import json
import sys
import time
from typing import Optional

from .paths import codex_home
from .store import load_store, locked, save_store, now
from .switcher import (
    best_target,
    do_switch,
    live_refresh_usage,
    snapshot_usage,
    sync_back,
)
from . import usage as usage_mod

EXIT_SWITCHED = 0
EXIT_ERROR = 1
EXIT_NOTHING = 2
EXIT_BLOCKED = 3


def _emit(json_mode: bool, kind: str, **fields) -> None:
    if json_mode:
        print(json.dumps({"event": kind, "ts": round(now(), 1), **fields}), flush=True)
    else:
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[auto] {kind} {detail}".rstrip(), flush=True)


def check_once(threshold: float, cooldown: float, dry_run: bool, json_mode: bool) -> int:
    with locked():
        store = load_store()
        home = codex_home()
        current = sync_back(store, home)
        if current is None:
            _emit(json_mode, "no-auth")
            save_store(store)
            return EXIT_NOTHING
        cur_acc = store.by_account_id(current.account_id)
        if cur_acc is None:
            _emit(json_mode, "unmanaged-account", email=current.label)
            save_store(store)
            return EXIT_NOTHING
        # Live quota first; rollout scan stays as the offline fallback.
        if not live_refresh_usage(store, cur_acc):
            snapshot_usage(store, cur_acc, home)
        if cur_acc.status == "needs_relogin":
            save_store(store)
            _emit(json_mode, "session-revoked", email=current.label)
            return EXIT_BLOCKED

        # Attribution is handled at snapshot time (snapshot_usage + the
        # last_switch_at fence), so the stored per-account snapshot is safe to
        # trust here even if it predates the last switch.
        usage = cur_acc.usage
        pct = usage_mod.worst_pct(usage) if usage else None
        if pct is None:
            _emit(json_mode, "no-usage-data", email=current.label)
            save_store(store)
            return EXIT_NOTHING
        if pct < threshold:
            _emit(json_mode, "below-threshold", email=current.label, used=round(pct, 1), threshold=threshold)
            save_store(store)
            return EXIT_NOTHING
        if store.last_switch_at and now() - store.last_switch_at < cooldown:
            _emit(json_mode, "cooldown", remaining=int(cooldown - (now() - store.last_switch_at)))
            save_store(store)
            return EXIT_NOTHING

        # Refresh candidates' quotas live before choosing where to land.
        for a in store.sorted_accounts():
            if a.account_id != current.account_id:
                live_refresh_usage(store, a)
        target = best_target(store, current.account_id, threshold=threshold)
        if target is None:
            _emit(json_mode, "blocked", reason="no viable target below threshold")
            save_store(store)
            return EXIT_BLOCKED

        if dry_run:
            _emit(json_mode, "would-switch", frm=current.label, to=target.display(), used=round(pct, 1))
            save_store(store)
            return EXIT_SWITCHED

        identity = do_switch(store, target, home)
        save_store(store)
        _emit(json_mode, "switched", frm=current.label, to=identity.label, used=round(pct, 1))
        return EXIT_SWITCHED


def run_auto(
    threshold: float,
    interval: float,
    cooldown: float,
    once: bool,
    dry_run: bool,
    json_mode: bool,
) -> int:
    if once:
        try:
            return check_once(threshold, cooldown, dry_run, json_mode)
        except Exception as e:  # noqa: BLE001
            _emit(json_mode, "error", message=str(e))
            return EXIT_ERROR

    _emit(json_mode, "start", threshold=threshold, interval=interval, cooldown=cooldown, dry_run=dry_run)
    while True:
        try:
            check_once(threshold, cooldown, dry_run, json_mode)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            _emit(json_mode, "error", message=str(e))
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0
