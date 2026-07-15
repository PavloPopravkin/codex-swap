"""OAuth token refresh against auth.openai.com.

Refresh tokens rotate on use: a successful refresh MUST be persisted
immediately or the account dies with "refresh token was already used".
A `refresh_token_invalidated` response means the session was revoked
server-side (logout, or a `codex login` on top of the credentials — codex
best-effort revokes the token it replaces) and only a fresh `codex login`
can revive the account.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.error
import urllib.request
from typing import Optional

from .auth import decode_jwt_claims

# codex CLI's public OAuth client id (from openai/codex source).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"


class RefreshError(Exception):
    """Transient refresh failure (network, 5xx) — token may still be good."""


class RefreshInvalidated(Exception):
    """Session revoked server-side — account needs `codex login`."""


def access_token_expires_in(auth: dict) -> Optional[float]:
    """Seconds until the access token expires (negative = expired)."""
    try:
        claims = decode_jwt_claims(auth["tokens"]["access_token"])
        return float(claims["exp"]) - time.time()
    except Exception:  # noqa: BLE001
        return None


def refresh_tokens(auth: dict) -> dict:
    """Refresh and return an UPDATED copy of the auth payload.

    Caller must persist the result immediately (rotation!).
    """
    tokens = auth.get("tokens") or {}
    rt = tokens.get("refresh_token")
    if not rt:
        raise RefreshError("no refresh_token in auth payload")
    body = json.dumps(
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "scope": "openid profile email",
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:  # noqa: BLE001
            pass
        if e.code in (400, 401) and (
            "refresh_token_invalidated" in detail or "invalid_grant" in detail
        ):
            raise RefreshInvalidated(f"session revoked (HTTP {e.code})") from e
        raise RefreshError(f"refresh failed: HTTP {e.code} {detail[:200]}") from e
    except (urllib.error.URLError, OSError) as e:
        raise RefreshError(f"refresh failed: {e}") from e

    updated = dict(auth)
    new_tokens = dict(tokens)
    if resp.get("access_token"):
        new_tokens["access_token"] = resp["access_token"]
    if resp.get("refresh_token"):
        new_tokens["refresh_token"] = resp["refresh_token"]
    if resp.get("id_token"):
        new_tokens["id_token"] = resp["id_token"]
    updated["tokens"] = new_tokens
    updated["last_refresh"] = (
        _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return updated
