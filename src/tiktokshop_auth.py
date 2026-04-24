"""
tiktokshop_auth.py
------------------
Manages the TikTok Shop access_token and refresh_token lifecycle.

Why this file exists:
  TikTok Shop tokens expire on a schedule. Instead of making the rest of the
  code care about expiry times, this module owns that problem. Callers only ask
  for "a valid access token" and this module checks freshness, refreshes when
  needed, and saves the updated token bundle back to disk.

  Tokens are stored in data/tiktokshop_tokens.json, which is committed back to
  the repo at the end of each run (same pattern as processed_orders.json).

Public functions (used by tiktokshop_client.py):
  - get_valid_access_token() -> string (an access token known to be fresh)

When refresh_token itself expires:
  The refresh call will fail with an error from TikTok Shop. This module raises
  RefreshTokenExpiredError, which main.py catches and reports via Telegram so
  the shop owner can manually re-authorize through TikTok Shop Open Platform.
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
    Raised when the refresh_token itself has expired and a human must
    re-authorize the app through TikTok Shop Open Platform.
    """
    pass


# ============================================================
# Public function (used by tiktokshop_client.py)
# ============================================================

def get_valid_access_token():
    """
    Returns an access token that is guaranteed to be fresh.

    If the stored access token is still valid (and will remain valid for at
    least TOKEN_REFRESH_BUFFER_MINUTES), returns it directly. Otherwise,
    refreshes it using the refresh_token and returns the new access token.

    Returns:
      A valid access_token string.

    Raises:
      RefreshTokenExpiredError: if the refresh_token has also expired and
        a human must manually re-authorize the app.
      FileNotFoundError: if the tokens file does not exist (first-run
        bootstrap not done yet).
    """

    # STEP 1: Load the tokens from disk.
    tokens = _load_tokens()

    # STEP 2: Check if the current access token is still fresh enough.
    if _is_access_token_fresh(tokens):
        print("  Access token is still fresh, using it directly")
        return tokens["access_token"]

    # STEP 3: Token is expired or about to expire. Refresh it.
    print("  Access token is expired or expiring soon, refreshing...")
    new_tokens = _refresh_tokens(tokens["refresh_token"])

    # STEP 4: Save the new tokens to disk for future runs.
    _save_tokens(new_tokens)
    print(f"  New tokens saved to {config.TOKENS_FILE_PATH}")

    return new_tokens["access_token"]


# ============================================================
# Internal helpers (file I/O)
# ============================================================

def _load_tokens():
    """
    Loads the tokens dictionary from disk.

    The tokens file has this format:
      {
        "access_token": "...",
        "refresh_token": "...",
        "access_token_expires_at": "2026-04-19T14:00:00+00:00"
      }

    Raises:
      FileNotFoundError: if the file does not exist. This means the very
        first-run bootstrap has not been done yet.
    """

    # STEP 1: Check that the file exists. If not, give a clear error message
    # so the developer knows exactly what to do.
    if not os.path.exists(config.TOKENS_FILE_PATH):
        raise FileNotFoundError(
            f"Tokens file not found at {config.TOKENS_FILE_PATH}. "
            f"You need to bootstrap initial tokens by authorizing the app "
            f"in TikTok Shop Open Platform and saving the resulting token "
            f"bundle to this file."
        )

    # STEP 2: Read and parse the JSON file.
    with open(config.TOKENS_FILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens):
    """
    Writes the tokens dictionary to disk with nice formatting.

    Args:
      tokens: dict with access_token, refresh_token, access_token_expires_at.
    """

    # STEP 1: Make sure the data folder exists.
    os.makedirs(os.path.dirname(config.TOKENS_FILE_PATH), exist_ok=True)

    # STEP 2: Write the JSON file with indentation so git diffs are readable.
    with open(config.TOKENS_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2, sort_keys=True)


# ============================================================
# Internal helpers (freshness check)
# ============================================================

def _is_access_token_fresh(tokens):
    """
    Returns True if the access token will still be valid after the
    configured refresh buffer, False if it needs to be refreshed.
    """

    # STEP 1: Parse the expiry timestamp from the tokens file.
    expires_at_iso = tokens.get("access_token_expires_at")
    if not expires_at_iso:
        return False

    expires_at = datetime.fromisoformat(expires_at_iso)

    # STEP 2: Compute the "must refresh before" cutoff time.
    buffer = timedelta(minutes=config.TOKEN_REFRESH_BUFFER_MINUTES)
    cutoff = datetime.now(timezone.utc) + buffer

    # STEP 3: Token is fresh if it expires AFTER the cutoff.
    return expires_at > cutoff


# ============================================================
# Internal helpers (the actual refresh API call)
# ============================================================

def _refresh_tokens(refresh_token):
    """
    Calls TikTok Shop's refresh endpoint to exchange the refresh_token for a
    fresh access_token and a fresh refresh_token.

    Args:
      refresh_token: the current refresh_token string.

    Returns:
      A dict with access_token, refresh_token, access_token_expires_at.

    Raises:
      RefreshTokenExpiredError: if TikTok Shop says the refresh_token is expired.
    """

    # STEP 1: Build the request body exactly as the public TikTok Shop Open API
    # collection expects it.
    path = "/api/token/refreshToken"
    url = f"{config.TIKTOKSHOP_AUTH_BASE_URL}{path}"
    body = {
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "app_secret": config.TIKTOKSHOP_APP_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    # STEP 2: Call the refresh endpoint.
    response = requests.post(url, json=body, timeout=30)
    data = _safe_json(response)

    # STEP 3: Check for TikTok Shop API errors first.
    if response.status_code != 200 or _has_api_error(data):
        message = _extract_error_message(data) or response.text
        lowered = message.lower()
        if "refresh" in lowered and ("expired" in lowered or "invalid" in lowered):
            raise RefreshTokenExpiredError(
                "TikTok Shop refresh_token is expired or invalid. Please "
                f"re-authorize the app. Error: {message}"
            )
        raise RuntimeError(f"TikTok Shop refresh failed: {message}")

    # STEP 4: Extract the new token bundle and compute the absolute expiry timestamp.
    payload = _extract_payload(data)
    new_access_token = payload["access_token"]
    new_refresh_token = payload.get("refresh_token", refresh_token)
    expire_in_seconds = int(
        payload.get("access_token_expire_in")
        or payload.get("expire_in")
        or payload.get("expires_in")
        or 0
    )

    if expire_in_seconds <= 0:
        # STEP 5: Be defensive. If TikTok Shop did not return expiry seconds,
        # force an early refresh next run rather than trusting a stale token.
        expires_at = datetime.now(timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expire_in_seconds)

    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "access_token_expires_at": expires_at.isoformat(),
    }


# ============================================================
# Internal helpers (response parsing)
# ============================================================

def _safe_json(response):
    """Returns parsed JSON if possible, else an empty dict."""
    try:
        return response.json()
    except ValueError:
        return {}



def _has_api_error(data):
    """
    Returns True if the response JSON looks like a TikTok Shop API error.
    """
    code = data.get("code")
    if code in (None, 0, "0"):
        return False
    return True



def _extract_error_message(data):
    """Pulls the most helpful error message we can find from the response."""
    return (
        data.get("message")
        or data.get("msg")
        or data.get("error")
        or data.get("error_message")
        or ""
    )



def _extract_payload(data):
    """
    Normalizes TikTok Shop responses into the object that actually contains the
    useful fields.
    """
    if isinstance(data.get("data"), dict):
        return data["data"]
    if isinstance(data.get("response"), dict):
        return data["response"]
    return data
