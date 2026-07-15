"""Reading, writing and decoding codex auth.json."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class AuthError(Exception):
    pass


@dataclass
class Identity:
    account_id: str
    email: Optional[str]
    plan: Optional[str]
    api_key_only: bool = False

    @property
    def label(self) -> str:
        return self.email or f"api-key:{self.account_id[:12]}"


def read_auth(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise AuthError(f"{path} is not valid JSON: {e}") from e


def write_auth_atomic(path: Path, data: dict) -> None:
    """Write auth.json atomically with 0600 perms (same dir → same filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".auth-swap-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def decode_jwt_claims(token: str) -> dict:
    """Decode JWT payload without verification (we only need identity claims)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:  # noqa: BLE001
        raise AuthError(f"cannot decode id_token: {e}") from e


def identity_from_auth(auth: dict) -> Identity:
    """Extract a stable identity from an auth.json payload."""
    tokens = auth.get("tokens") or {}
    id_token = tokens.get("id_token")
    if id_token:
        claims = decode_jwt_claims(id_token)
        oa = claims.get("https://api.openai.com/auth") or {}
        account_id = (
            tokens.get("account_id")
            or oa.get("chatgpt_account_id")
            or claims.get("sub")
        )
        if not account_id:
            raise AuthError("auth.json has tokens but no account id")
        return Identity(
            account_id=str(account_id),
            email=claims.get("email"),
            plan=oa.get("chatgpt_plan_type"),
            api_key_only=False,
        )

    api_key = auth.get("OPENAI_API_KEY")
    if api_key:
        fp = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        return Identity(account_id=f"apikey-{fp}", email=None, plan=None, api_key_only=True)

    raise AuthError("auth.json has neither ChatGPT tokens nor an API key")


def last_refresh(auth: dict) -> str:
    """Sortable freshness marker (ISO timestamp string, '' if absent)."""
    return auth.get("last_refresh") or ""
