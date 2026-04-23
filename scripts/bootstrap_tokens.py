"""
bootstrap_tokens.py
-------------------
One-time script to seed data/tiktokshop_tokens.json with initial tokens.

When to run this:
  - The very first time you set up the bot.
  - After the refresh_token expires and you need to re-authorize.
  - When switching apps or environments.

How to use it:
  1. Log into TikTok Shop Open Platform.
  2. Authorize your app for the target shop.
  3. After redirect, copy the auth_code value from the URL.
  4. Run this script and paste the auth_code when prompted.
  5. The script writes data/tiktokshop_tokens.json with valid tokens.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Add project root to path so we can import src.config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config



def main():
    """Interactive bootstrap flow."""

    print("=" * 60)
    print("TikTok Shop Tokens Bootstrap")
    print("=" * 60)
    print(f"Target environment: {config.TIKTOKSHOP_AUTH_BASE_URL}")
    print(f"App Key: {config.TIKTOKSHOP_APP_KEY}")
    print(f"Shop ID: {config.TIKTOKSHOP_SHOP_ID}")
    print()

    # STEP 1: Prompt for the authorization code.
    auth_code = input("Paste the auth_code from the TikTok Shop redirect URL: ").strip()
    if not auth_code:
        print("No auth_code provided. Aborting.")
        sys.exit(1)

    # STEP 2: Build the request param exactly as the public TikTok Shop Open API
    # collection expects it.
    path = "/api/v2/token/get"
    url = f"{config.TIKTOKSHOP_AUTH_BASE_URL}{path}"
    params = {
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "app_secret": config.TIKTOKSHOP_APP_SECRET,
        "auth_code": auth_code,
        "grant_type": "authorized_code",
    }

    # STEP 3: Call the endpoint.
    print(f"\nCalling {path}...")
    response = requests.get(url, params=params, timeout=30)
    print(f"Status: {response.status_code}")
    data = _safe_json(response)

    # STEP 4: Check for errors.
    if response.status_code != 200 or _has_api_error(data):
        print("\nTikTok Shop rejected the request:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        sys.exit(1)

    # STEP 5: Extract the tokens and compute the absolute expiry timestamp.
    payload = _extract_payload(data)
    access_token = payload["access_token"]
    refresh_token = payload["refresh_token"]
    expire_in_seconds = int(
        payload.get("access_token_expire_in")
        or payload.get("expire_in")
        or payload.get("expires_in")
        or 0
    )
    if expire_in_seconds <= 0:
        expires_at = datetime.now(timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expire_in_seconds)

    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expires_at": expires_at.isoformat(),
    }

    # STEP 6: Write the tokens file.
    tokens_path = Path(config.TOKENS_FILE_PATH)
    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tokens_path, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2, sort_keys=True)

    print(f"\n✓ Wrote tokens to {tokens_path}")
    print(f"  access_token:  {access_token[:12]}... (truncated)")
    print(f"  refresh_token: {refresh_token[:12]}... (truncated)")
    print(f"  expires at:    {expires_at.isoformat()}")



def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {}



def _has_api_error(data):
    code = data.get("code")
    if code in (None, 0, "0"):
        return False
    return True



def _extract_payload(data):
    if isinstance(data.get("data"), dict):
        return data["data"]
    if isinstance(data.get("response"), dict):
        return data["response"]
    return data


if __name__ == "__main__":
    main()
