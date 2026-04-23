"""
get_chiper_code.py
------------------
Fetches the authorized shops list from TikTok Shop and prints the full
response body so we can see the shop cipher code.

Why this script exists:
  After the OAuth/token bootstrap, TikTok Shop gives us an access token,
  but some shop-level APIs want the shop "cipher" value. This script calls
  the authorized shops endpoint, prints the raw body for debugging, and then
  highlights the matching shop based on TIKTOKSHOP_SHOP_ID.

How to use it:
  1. Make sure these environment variables are set:
       - TIKTOKSHOP_APP_KEY
       - TIKTOKSHOP_APP_SECRET
       - TIKTOKSHOP_SHOP_ID
  2. Make sure data/tiktokshop_tokens.json already exists and contains a
     valid access_token.
  3. Run:
       python scripts/get_chiper_code.py
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

# Add project root to path so we can import src.config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config


# TikTok Shop's authorized shops endpoint lives on the open-api host,
# not the auth host used during token bootstrap.
_OPEN_API_BASE_URL = "https://open-api.tiktokglobalshop.com"


def main():
    """Entry point for the helper script."""

    print("=" * 60)
    print("TikTok Shop - Get Authorized Shops / Cipher Code")
    print("=" * 60)
    print(f"App Key: {config.TIKTOKSHOP_APP_KEY}")
    print(f"Target Shop ID: {config.TIKTOKSHOP_SHOP_ID}")
    print()

    # STEP 1: Load the access token from the existing tokens file.
    access_token = _load_access_token()
    print(f"Loaded access token from {config.TOKENS_FILE_PATH}")

    # STEP 2: Build the signed request exactly as TikTok Shop expects.
    path = "/authorization/202309/shops"
    timestamp = str(int(time.time()))

    # Important: build the signature using ONLY app_key, timestamp, and version.
    # The access_token is added AFTER the signature is computed.
    params_for_sign = {
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "timestamp": timestamp,
        "version": "202309",
    }
    sign = _make_signature(path=path, params=params_for_sign)

    # STEP 3: Add the remaining request parameters used in the actual call.
    request_params = {
        **params_for_sign,
        "access_token": access_token,
        "sign": sign,
    }

    url = f"{_OPEN_API_BASE_URL}{path}"
    headers = {
        "x-tts-access-token": access_token,
        "content-type": "application/json",
    }

    print(f"Calling {path} ...")
    print(f"GET {url}?{urlencode(request_params)}")

    # STEP 4: Call the endpoint.
    response = requests.get(url, params=request_params, headers=headers, timeout=30)
    print(f"HTTP status: {response.status_code}")

    # STEP 5: Show the raw response body exactly as returned by TikTok Shop.
    print("\nRaw response body:")
    print(response.text)

    # STEP 6: Try to parse JSON so we can also highlight the matching shop.
    try:
        data = response.json()
    except ValueError:
        print("\nResponse is not valid JSON. Stopping here.")
        sys.exit(1)

    if response.status_code != 200:
        print("\nTikTok Shop returned a non-200 HTTP status.")
        sys.exit(1)

    if data.get("code") not in (0, "0"):
        print("\nTikTok Shop returned an API error:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        sys.exit(1)

    # STEP 7: Extract shops and look for the configured shop_id.
    shops = data.get("data", {}).get("shops", [])
    if not shops:
        print("\nNo shops found in response.")
        return

    print("\nAuthorized shops summary:")
    for shop in shops:
        print(
            f"  - id={shop.get('id')} | code={shop.get('code')} | "
            f"cipher={shop.get('cipher')} | name={shop.get('name')}"
        )

    matching_shop = next(
        (shop for shop in shops if str(shop.get("id")) == str(config.TIKTOKSHOP_SHOP_ID)),
        None,
    )

    # STEP 8: Print the most useful final result.
    if matching_shop:
        print("\nMatched shop:")
        print(json.dumps(matching_shop, indent=2, ensure_ascii=False))
        print(f"\nCipher code for shop {config.TIKTOKSHOP_SHOP_ID}: {matching_shop.get('cipher')}")
    else:
        print(
            "\nConfigured TIKTOKSHOP_SHOP_ID was not found in the authorized shops list."
        )
        print("Check whether the correct shop was authorized for this access token.")


def _load_access_token():
    """
    Loads the access token from data/tiktokshop_tokens.json.

    Supports both of these shapes just in case:
      1. {"access_token": "..."}
      2. {"data": {"access_token": "..."}}
    """

    tokens_path = Path(config.TOKENS_FILE_PATH)
    if not tokens_path.exists():
        raise FileNotFoundError(
            f"Tokens file not found at {tokens_path}. Run bootstrap_tokens.py first."
        )

    with open(tokens_path, "r", encoding="utf-8") as f:
        token_data = json.load(f)

    # STEP 1: Prefer the flat structure used by this repo's bootstrap script.
    access_token = token_data.get("access_token")
    if access_token:
        return access_token

    # STEP 2: Fall back to nested structure if the file came from another tool.
    nested_access_token = token_data.get("data", {}).get("access_token")
    if nested_access_token:
        return nested_access_token

    raise ValueError(f"No access_token found in {tokens_path}.")


def _make_signature(path, params):
    """
    Builds TikTok Shop's signature for /authorization/202309/shops.

    The string-to-sign format is:
      app_secret + path + concatenated(sorted_query_params) + app_secret

    Then that string is HMAC-SHA256 signed using app_secret as the key,
    and encoded as lowercase hex.

    Important:
      access_token is NOT included in this signature for this endpoint.
    """

    # STEP 1: Sort query parameters by name, ascending, case sensitive.
    sorted_items = sorted(params.items(), key=lambda item: item[0])

    # STEP 2: Build the exact string TikTok Shop expects.
    parts = [config.TIKTOKSHOP_APP_SECRET, path]
    for key, value in sorted_items:
        parts.append(str(key))
        parts.append(str(value))
    parts.append(config.TIKTOKSHOP_APP_SECRET)
    string_to_sign = "".join(parts)

    # STEP 3: Compute HMAC-SHA256 and return lowercase hex.
    return hmac.new(
        config.TIKTOKSHOP_APP_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


if __name__ == "__main__":
    main()