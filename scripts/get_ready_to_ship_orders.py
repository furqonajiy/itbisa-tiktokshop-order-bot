"""
get_ready_to_ship_orders.py
---------------------------
Fetches ALL TikTok Shop orders that are ready to ship.

What this script does:
  1. Loads the access_token from data/tiktokshop_tokens.json.
  2. Reads shop_cipher from environment, or prompts for it.
  3. Calls TikTok Shop's order search endpoint page by page.
  4. Filters by order_status = AWAITING_SHIPMENT.
  5. Saves the combined result to data/ready_to_ship_orders.json.

Why this is a standalone script:
  The current branch already has bootstrap_tokens.py that saves tokens,
  but the repo does not yet have a dedicated TikTok Shop order client.
  This script lets you test order retrieval immediately without redesigning
  the whole app first.
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import requests

# Add project root to path so we can import src.config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config


# STEP 0: Constants used only by this script.
# We keep them local so we do not need to modify src/config.py yet.
TIKTOKSHOP_OPEN_API_BASE_URL = "https://open-api.tiktokglobalshop.com"
ORDER_SEARCH_PATH = "/order/202309/orders/search"
DEFAULT_PAGE_SIZE = 50
OUTPUT_FILE_PATH = PROJECT_ROOT / "data" / "ready_to_ship_orders.json"


def main():
    """Fetch every page of ready-to-ship orders and save them locally."""

    print("=" * 60)
    print("TikTok Shop - Get Ready To Ship Orders")
    print("=" * 60)
    print(f"App Key: {config.TIKTOKSHOP_APP_KEY}")
    print(f"Open API Base URL: {TIKTOKSHOP_OPEN_API_BASE_URL}")
    print()

    # STEP 1: Load the access token from the tokens file written by bootstrap.
    access_token = _load_access_token()

    # STEP 2: Read shop_cipher from environment, or prompt once if missing.
    # Your curl example uses shop_cipher, and the current config.py does not
    # store it yet, so prompting keeps this script easy to test immediately.
    shop_cipher = os.environ.get("TIKTOKSHOP_SHOP_CIPHER", "").strip()
    if not shop_cipher:
        shop_cipher = input("Paste your TikTok Shop shop_cipher: ").strip()

    if not shop_cipher:
        print("No shop_cipher provided. Aborting.")
        sys.exit(1)

    # STEP 3: Paginate until TikTok Shop stops returning a next page token.
    all_orders = []
    page_token = None
    page_number = 1

    while True:
        print(f"\nFetching page {page_number}...")
        page_result = _fetch_orders_page(
            access_token=access_token,
            shop_cipher=shop_cipher,
            page_size=DEFAULT_PAGE_SIZE,
            page_token=page_token,
        )

        orders = page_result["orders"]
        next_page_token = page_result["next_page_token"]
        has_more = page_result["has_more"]

        print(f"  Orders returned on this page: {len(orders)}")

        all_orders.extend(orders)

        # STEP 4: Stop when there is no next page.
        if not has_more or not next_page_token:
            break

        page_token = next_page_token
        page_number += 1

        # STEP 5: Small pause to be polite to the API.
        time.sleep(0.3)

    # STEP 6: Save the combined result for inspection and later use.
    OUTPUT_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(all_orders, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print(f"Done. Total ready-to-ship orders fetched: {len(all_orders)}")
    print(f"Saved to: {OUTPUT_FILE_PATH}")
    print("=" * 60)


def _load_access_token():
    """
    Reads the access_token from data/tiktokshop_tokens.json.

    We only need the current access token for this script.
    """
    # STEP 1: Make sure the tokens file exists.
    tokens_path = Path(config.TOKENS_FILE_PATH)
    if not tokens_path.exists():
        raise FileNotFoundError(
            f"Tokens file not found at {tokens_path}. "
            f"Run scripts/bootstrap_tokens.py first."
        )

    # STEP 2: Parse the JSON file.
    with open(tokens_path, "r", encoding="utf-8") as f:
        tokens = json.load(f)

    access_token = tokens.get("access_token", "").strip()
    if not access_token:
        raise RuntimeError(
            f"No access_token found in {tokens_path}. "
            f"Re-run bootstrap if needed."
        )

    return access_token


def _fetch_orders_page(access_token, shop_cipher, page_size, page_token=None):
    """
    Fetches one page of AWAITING_SHIPMENT orders.

    Returns a dict:
      {
        "orders": [...],
        "next_page_token": "... or None",
        "has_more": True/False
      }
    """

    # STEP 1: Build the body.
    # AWAITING_SHIPMENT is the "ready to ship" state.
    body = {
        "order_status": "AWAITING_SHIPMENT",
    }

    # STEP 2: Build the query params exactly in the style of your curl example.
    timestamp = int(time.time())
    query_params = {
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "page_size": str(page_size),
        "timestamp": str(timestamp),
        "shop_cipher": shop_cipher,
        "sort_field": "create_time",
        "sort_order": "ASC",
    }

    if page_token:
        query_params["page_token"] = page_token

    # STEP 3: Create the signature and attach it as query param.
    sign = _make_signature(
        path=ORDER_SEARCH_PATH,
        query_params=query_params,
        body=body,
        app_secret=config.TIKTOKSHOP_APP_SECRET,
    )
    query_params["sign"] = sign

    # STEP 4: Build the request URL and headers.
    url = f"{TIKTOKSHOP_OPEN_API_BASE_URL}{ORDER_SEARCH_PATH}"
    headers = {
        "x-tts-access-token": access_token,
        "content-type": "application/json",
    }

    # STEP 5: Send the request.
    response = requests.post(
        url,
        params=query_params,
        headers=headers,
        json=body,
        timeout=30,
    )

    # STEP 6: Parse and validate the response.
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"TikTok Shop did not return valid JSON. "
            f"Status={response.status_code}, Body={response.text}"
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"TikTok Shop HTTP error. Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}"
        )

    api_code = data.get("code")
    if api_code not in (0, "0", None):
        raise RuntimeError(
            f"TikTok Shop API error. Code={api_code}, Body={json.dumps(data, ensure_ascii=False)}"
        )

    # STEP 7: Extract the payload.
    payload = data.get("data", {})

    # TikTok Shop response field names may differ slightly by API version.
    # We support the common shapes so the script is more resilient.
    orders = (
            payload.get("orders")
            or payload.get("order_list")
            or payload.get("list")
            or []
    )

    next_page_token = (
            payload.get("next_page_token")
            or payload.get("page_token")
            or payload.get("nextPageToken")
    )

    has_more = bool(
        payload.get("more")
        or payload.get("has_more")
        or payload.get("hasMore")
        or next_page_token
    )

    return {
        "orders": orders,
        "next_page_token": next_page_token,
        "has_more": has_more,
    }


def _make_signature(path, query_params, body, app_secret):
    """
    Builds the TikTok Shop request signature.

    This follows the common TTS pattern used by working client examples:
      1. Exclude 'sign' and 'access_token' from the query params.
      2. Sort remaining query params by key.
      3. Concatenate as key + value.
      4. Prepend the request path.
      5. Append compact JSON body for non-empty JSON requests.
      6. Wrap with app_secret at both ends.
      7. HMAC-SHA256 with app_secret and return lowercase hex.
    """

    # STEP 1: Exclude fields that should not participate in signing.
    filtered_params = {}
    for key, value in query_params.items():
        if key in ("sign", "access_token"):
            continue
        if value is None or value == "":
            continue
        filtered_params[key] = str(value)

    # STEP 2: Sort parameters alphabetically and concatenate key+value.
    param_string = "".join(
        f"{key}{filtered_params[key]}"
        for key in sorted(filtered_params.keys())
    )

    # STEP 3: Compact JSON body so the signature input is stable.
    body_string = ""
    if body:
        body_string = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    # STEP 4: Build the string-to-sign.
    string_to_sign = f"{path}{param_string}{body_string}"
    wrapped_string = f"{app_secret}{string_to_sign}{app_secret}"

    # STEP 5: HMAC-SHA256 -> lowercase hex.
    signature = hmac.new(
        app_secret.encode("utf-8"),
        wrapped_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return signature


if __name__ == "__main__":
    main()