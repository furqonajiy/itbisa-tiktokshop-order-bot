"""
tiktokshop_auth.py
------------------
Manages the TikTok Shop access_token and refresh_token lifecycle.

Why this file exists:
  Access tokens expire on a schedule, refresh tokens eventually expire too.
  Instead of making the rest of the code care about expiry, this module owns
  that problem. Callers just ask for "a valid access token" and this module
  checks freshness, refreshes when needed, and persists the updated bundle.

  Tokens are stored in data/tiktokshop_tokens.json, which is committed to the
  bot-state branch at the end of each run.

Public functions (used by tiktokshop_client.py):
  - get_valid_access_token() -> string

When refresh_token itself expires:
  We raise RefreshTokenExpiredError, which main.py catches and reports via
  Telegram so the shop owner can re-authorize the app manually.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from src import config


# ============================================================
# Custom exception for the one case that needs human intervention
# ============================================================

class RefreshTokenExpiredError(Exception):
    """
    Raised when the refresh_token has expired and a human must re-authorize
    the app through the TikTok Shop Open Platform.
    """
    pass


# ============================================================
# Public function (used by tiktokshop_client.py)
# ============================================================

def get_valid_access_token():
    """
    Returns an access token that is guaranteed to be fresh.

    If the stored access_token is still valid for at least
    TOKEN_REFRESH_BUFFER_MINUTES, returns it directly. Otherwise refreshes
    it using the refresh_token, saves the new bundle, and returns the new
    access_token.

    Raises:
      RefreshTokenExpiredError: if the refresh_token itself is also expired.
      FileNotFoundError: if the tokens file has never been bootstrapped.
    """

    # STEP 1: Load the tokens from disk.
    tokens = _load_tokens()

    # STEP 2: Fast path - access_token is still fresh enough.
    if _is_access_token_fresh(tokens):
        print("  Access token is still fresh, using it directly")
        return tokens["access_token"]

    # STEP 3: Refresh needed. First make sure refresh_token itself hasn't expired.
    _guard_refresh_token_expiry(tokens)

    # STEP 4: Call the TikTok Shop refresh endpoint.
    print("  Access token is expired or expiring soon, refreshing...")
    new_tokens = _refresh_tokens(tokens)

    # STEP 5: Save the updated bundle so future runs pick up the new values.
    _save_tokens(new_tokens)
    print(f"  New tokens saved to {config.TOKENS_FILE_PATH}")

    return new_tokens["access_token"]


# ============================================================
# Internal helpers (file I/O)
# ============================================================

def _load_tokens():
    """
    Loads the tokens dictionary from disk.

    Expected file shape (produced by bootstrap_tokens.py):
      {
        "access_token": "...",
        "access_token_expires_at": "2026-04-30T21:14:00+00:00",
        "refresh_token": "...",
        "refresh_token_expires_at": "2125-03-29T19:21:38+00:00"
      }
    """

    if not os.path.exists(config.TOKENS_FILE_PATH):
        raise FileNotFoundError(
            f"Tokens file not found at {config.TOKENS_FILE_PATH}. "
            f"Run scripts/bootstrap_tokens.py to seed initial tokens first."
        )

    with open(config.TOKENS_FILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens):
    """Writes the tokens dictionary to disk with readable formatting."""

    os.makedirs(os.path.dirname(config.TOKENS_FILE_PATH), exist_ok=True)
    with open(config.TOKENS_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2, sort_keys=True)


# ============================================================
# Internal helpers (freshness checks)
# ============================================================

def _is_access_token_fresh(tokens):
    """
    Returns True if the access_token will still be valid after the configured
    refresh buffer. Refreshing before the actual deadline avoids the risk of
    a token expiring mid-flight during an API call.
    """

    expires_at_iso = tokens.get("access_token_expires_at")
    if not expires_at_iso:
        return False

    expires_at = datetime.fromisoformat(expires_at_iso)
    buffer = timedelta(minutes=config.TOKEN_REFRESH_BUFFER_MINUTES)
    cutoff = datetime.now(timezone.utc) + buffer

    return expires_at > cutoff


def _guard_refresh_token_expiry(tokens):
    """
    Raises RefreshTokenExpiredError if the refresh_token itself has expired.
    Checking locally first saves one network call and gives a cleaner error.
    """

    expires_at_iso = tokens.get("refresh_token_expires_at")
    if not expires_at_iso:
        # No expiry info stored. Proceed and let TikTok Shop tell us.
        return

    expires_at = datetime.fromisoformat(expires_at_iso)
    if expires_at <= datetime.now(timezone.utc):
        raise RefreshTokenExpiredError(
            f"refresh_token expired on {expires_at_iso}. "
            f"Re-run scripts/bootstrap_tokens.py to re-authorize."
        )


# ============================================================
# Internal helpers (the actual refresh API call)
# ============================================================

def _refresh_tokens(current_tokens):
    """
    Calls TikTok Shop's refresh endpoint to exchange the refresh_token for a
    fresh token bundle.

    TikTok Shop returns Unix timestamps (seconds since epoch) for expiry,
    which we convert to ISO 8601 strings to match the bootstrap script's
    file format.
    """

    # STEP 1: Build the request, same style as bootstrap (GET + query params).
    path = "/api/v2/token/refresh"
    url = f"{config.TIKTOKSHOP_AUTH_BASE_URL}{path}"
    params = {
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "app_secret": config.TIKTOKSHOP_APP_SECRET,
        "refresh_token": current_tokens["refresh_token"],
        "grant_type": "refresh_token",
    }

    # STEP 2: Call the endpoint.
    response = requests.get(url, params=params, timeout=30)

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"TikTok Shop refresh did not return valid JSON. "
            f"Status={response.status_code}, Body={response.text}"
        )

    # STEP 3: Validate response. Raise RefreshTokenExpiredError for the specific
    # case where the refresh_token itself is no longer valid.
    if response.status_code != 200 or data.get("code") not in (0, "0", None):
        message = str(
            data.get("message") or data.get("msg") or data.get("error") or ""
        )
        lowered = message.lower()
        if "refresh" in lowered and ("expired" in lowered or "invalid" in lowered):
            raise RefreshTokenExpiredError(
                f"TikTok Shop refresh_token is expired or invalid. "
                f"Please re-authorize the app. Error: {message or data}"
            )
        raise RuntimeError(f"TikTok Shop refresh failed: {data}")

    # STEP 4: Extract and normalize the new bundle.
    payload = data.get("data") or {}
    new_access_token = payload.get("access_token")
    new_refresh_token = payload.get("refresh_token") or current_tokens["refresh_token"]
    access_token_expire_in = payload.get("access_token_expire_in")
    refresh_token_expire_in = payload.get("refresh_token_expire_in")

    if not new_access_token:
        raise RuntimeError(f"TikTok Shop refresh response missing access_token: {data}")

    # STEP 5: TikTok Shop returns expiry as Unix timestamps, not durations.
    # Keep the existing refresh_token_expires_at if TikTok Shop didn't echo a
    # new one back (it usually does, but better safe than to lose the info).
    access_expires_at_iso = _unix_to_iso(access_token_expire_in)
    refresh_expires_at_iso = (
        _unix_to_iso(refresh_token_expire_in)
        if refresh_token_expire_in
        else current_tokens.get("refresh_token_expires_at")
    )

    return {
        "access_token": new_access_token,
        "access_token_expires_at": access_expires_at_iso,
        "refresh_token": new_refresh_token,
        "refresh_token_expires_at": refresh_expires_at_iso,
    }


def _unix_to_iso(value):
    """Converts a Unix timestamp in seconds to an ISO 8601 UTC string."""
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()