"""Rate-limit / usage extraction from codex session rollout files.

Codex records a `rate_limits` object (primary = 5h window, secondary = weekly,
or a single weekly window depending on plan) in token_count events inside
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. No network calls needed — we scan
the newest rollouts and take the most recent snapshot.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Optional, Tuple

MAX_FILES_TO_SCAN = 25


def _rollout_files(codex_home: Path) -> list:
    root = codex_home / "sessions"
    if not root.is_dir():
        return []
    files = list(root.glob("*/*/*/rollout-*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _parse_ts(ts: str) -> Optional[float]:
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def latest_rate_limits(
    codex_home: Path, since: float = 0.0
) -> Optional[Tuple[float, dict]]:
    """Newest (timestamp, rate_limits) found in recent rollouts, or None.

    Only events strictly newer than `since` are returned — callers pass the
    last-switch time so usage from a previous account is never misattributed.
    """
    best: Optional[Tuple[float, dict]] = None
    for path in _rollout_files(codex_home)[:MAX_FILES_TO_SCAN]:
        found: Optional[Tuple[float, dict]] = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload") or {}
                    rl = payload.get("rate_limits")
                    if not rl:
                        continue
                    ts = _parse_ts(obj.get("timestamp") or "") or path.stat().st_mtime
                    found = (ts, rl)
        except OSError:
            continue
        if found and (best is None or found[0] > best[0]):
            best = found
        # Files are mtime-sorted newest-first, so the first file that yields a
        # snapshot holds the newest one — stop scanning.
        if best:
            break
    if best and best[0] <= since:
        return None
    return best


def window_pct(rl: dict, key: str) -> Optional[float]:
    win = rl.get(key)
    if not isinstance(win, dict):
        return None
    v = win.get("used_percent")
    return float(v) if v is not None else None


def worst_pct(rl: dict) -> Optional[float]:
    vals = [v for v in (window_pct(rl, "primary"), window_pct(rl, "secondary")) if v is not None]
    return max(vals) if vals else None


def _fmt_window_minutes(mins) -> str:
    try:
        mins = int(mins)
    except (TypeError, ValueError):
        return "?"
    if mins % 10080 == 0:
        return f"{mins // 10080}w"
    if mins % 1440 == 0:
        return f"{mins // 1440}d"
    if mins % 60 == 0:
        return f"{mins // 60}h"
    return f"{mins}m"


def _fmt_resets(resets_at) -> str:
    try:
        delta = int(resets_at) - _dt.datetime.now().timestamp()
    except (TypeError, ValueError):
        return ""
    if delta <= 0:
        return "reset"
    days, rem = divmod(int(delta), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"resets {days}d{hours}h"
    if hours:
        return f"resets {hours}h{minutes}m"
    return f"resets {minutes}m"


def describe(rl: Optional[dict], age: Optional[float] = None) -> str:
    """One-line human summary of a rate_limits snapshot."""
    if not rl:
        return "no usage data"
    parts = []
    for key in ("primary", "secondary"):
        win = rl.get(key)
        if not isinstance(win, dict):
            continue
        pct = win.get("used_percent")
        label = _fmt_window_minutes(win.get("window_minutes"))
        resets = _fmt_resets(win.get("resets_at"))
        seg = f"{label}: {pct:.0f}%" if pct is not None else f"{label}: ?"
        if resets:
            seg += f" ({resets})"
        parts.append(seg)
    if not parts:
        return "no usage data"
    line = ", ".join(parts)
    if age is not None:
        if age > 172800:
            line += f" [stale {int(age // 86400)}d]"
        elif age > 7200:
            line += f" [stale {int(age // 3600)}h]"
    return line


def is_reset(usage: Optional[dict], usage_at: Optional[float]) -> bool:
    """True when every window in the snapshot has already reset."""
    if not usage:
        return False
    now = _dt.datetime.now().timestamp()
    windows = [usage.get(k) for k in ("primary", "secondary") if isinstance(usage.get(k), dict)]
    if not windows:
        return False
    return all(
        isinstance(w.get("resets_at"), (int, float)) and w["resets_at"] < now
        for w in windows
    )
