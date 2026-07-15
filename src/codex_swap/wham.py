"""Live usage from the ChatGPT backend (`wham/usage`).

Undocumented endpoint used by several community switchers — gives fresh
rate-limit windows for ANY stored account without codex running. Rollout
scanning stays as the offline fallback.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class WhamError(Exception):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def fetch_usage(auth: dict) -> dict:
    """Raw wham/usage response for the given auth payload."""
    tokens = auth.get("tokens") or {}
    at = tokens.get("access_token")
    aid = tokens.get("account_id")
    if not at:
        raise WhamError("no access_token")
    headers = {"Authorization": f"Bearer {at}", "User-Agent": "codex-cli"}
    if aid:
        headers["chatgpt-account-id"] = aid
    req = urllib.request.Request(USAGE_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise WhamError(f"HTTP {e.code}", code=e.code) from e
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise WhamError(str(e)) from e


def _window(win: Optional[dict]) -> Optional[dict]:
    if not isinstance(win, dict):
        return None
    secs = win.get("limit_window_seconds")
    return {
        "used_percent": win.get("used_percent"),
        "window_minutes": int(secs / 60) if secs else None,
        "resets_at": win.get("reset_at"),
    }


def to_snapshot(raw: dict) -> Optional[dict]:
    """Map a wham/usage response onto the rollout rate_limits shape, so the
    rest of the code (describe/worst_pct/is_reset) works on one format."""
    rl = raw.get("rate_limit")
    if not isinstance(rl, dict):
        return None
    snap = {
        "primary": _window(rl.get("primary_window")),
        "secondary": _window(rl.get("secondary_window")),
        "plan_type": raw.get("plan_type"),
        "source": "wham",
    }
    if snap["primary"] is None and snap["secondary"] is None:
        return None
    return snap
