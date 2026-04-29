"""
tiktokshop_auth.py
------------------
Manages the access_token and refresh_token lifecycle.

Tokens live in data/tiktokshop_tokens.json and are committed to the bot-state
branch at the end of each run. The one public function is
get_valid_access_token, which returns a token guaranteed fresh for at least
TOKEN_REFRESH_BUFFER_MINUTES.

If the refresh_token itself has expired, we raise RuntimeError with a clear
message so main.py can forward it to Telegram. The operator then has to
re-authorize via scripts/bootstrap_tokens.py.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from src import config


def get_valid_access_token():
    """Returns a fresh access token. Refreshes if needed."""

    tokens = _load_tokens()

    if _is_access_token_fresh(tokens):
        print("  Access token is still fresh, using it directly")
        return tokens["access_token"]

    _guard_refresh_token_expiry(tokens)

    print("  Access token expired or expiring soon, refreshing...")
    new_tokens = _refresh_tokens(tokens)
    _save_tokens(new_tokens)
    print(f"  New tokens saved to {config.TOKENS_FILE_PATH}")

    return new_tokens["access_token"]


# ============================================================
# Internal: file I/O
# ============================================================

def _load_tokens():
    if not os.path.exists(config.TOKENS_FILE_PATH):
        raise FileNotFoundError(
            f"Tokens file not found at {config.TOKENS_FILE_PATH}. "
            f"Run scripts/bootstrap_tokens.py first."
        )

    with open(config.TOKENS_FILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens):
    os.makedirs(os.path.dirname(config.TOKENS_FILE_PATH), exist_ok=True)
    _atomic_write_json(config.TOKENS_FILE_PATH, tokens)


# ============================================================
# Internal: freshness checks
# ============================================================

def _is_access_token_fresh(tokens):
    """True if access_token is valid past the refresh buffer window."""
    expires_at = datetime.fromisoformat(tokens["access_token_expires_at"])
    buffer = timedelta(minutes=config.TOKEN_REFRESH_BUFFER_MINUTES)
    return expires_at > datetime.now(timezone.utc) + buffer


def _guard_refresh_token_expiry(tokens):
    """Fails fast if the refresh_token itself is also dead."""
    expires_at = datetime.fromisoformat(tokens["refresh_token_expires_at"])
    if expires_at <= datetime.now(timezone.utc):
        raise RuntimeError(
            f"Refresh token expired on {tokens['refresh_token_expires_at']}. "
            f"Re-run scripts/bootstrap_tokens.py to re-authorize."
        )


# ============================================================
# Internal: actual refresh API call
# ============================================================

def _refresh_tokens(current_tokens):
    """Exchanges refresh_token for a fresh token bundle."""

    url = f"{config.TIKTOKSHOP_AUTH_BASE_URL}/api/v2/token/refresh"
    params = {
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "app_secret": config.TIKTOKSHOP_APP_SECRET,
        "refresh_token": current_tokens["refresh_token"],
        "grant_type": "refresh_token",
    }

    response = requests.get(url, params=params, timeout=30)
    data = response.json()

    if data["code"] != 0:
        message = data.get("message", "")
        # Most common cause: refresh_token itself is no longer valid.
        if "expired" in message.lower() or "invalid" in message.lower():
            raise RuntimeError(
                f"TikTok Shop rejected refresh_token ({message}). "
                f"Re-run scripts/bootstrap_tokens.py to re-authorize."
            )
        raise RuntimeError(f"Token refresh failed: {data}")

    payload = data["data"]
    return {
        "access_token": payload["access_token"],
        "access_token_expires_at": _unix_to_iso(payload["access_token_expire_in"]),
        "refresh_token": payload["refresh_token"],
        "refresh_token_expires_at": _unix_to_iso(payload["refresh_token_expire_in"]),
    }


def _unix_to_iso(value):
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()


def _atomic_write_json(path, data):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)