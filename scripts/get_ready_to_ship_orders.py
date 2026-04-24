"""
get_ready_to_ship_orders.py
---------------------------
Fetches ALL TikTok Shop orders in either:
  - AWAITING_SHIPMENT
  - AWAITING_COLLECTION

What this script does:
  1. Loads the access_token from data/tiktokshop_tokens.json.
  2. Reads shop_cipher from environment, or prompts for it.
  3. Calls TikTok Shop's order search endpoint page by page for BOTH statuses.
  4. Merges the results and removes duplicates by order_id.
  5. Prints a readable list of all matching orders.
  6. Saves the combined result to data/ready_to_ship_orders.json.
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
TIKTOKSHOP_OPEN_API_BASE_URL = "https://open-api.tiktokglobalshop.com"
ORDER_SEARCH_PATH = "/order/202309/orders/search"
DEFAULT_PAGE_SIZE = 50
OUTPUT_FILE_PATH = PROJECT_ROOT / "data" / "ready_to_ship_orders.json"

# STEP 0b: We want both statuses.
TARGET_STATUSES = [
    "AWAITING_SHIPMENT",
    "AWAITING_COLLECTION",
]


def main():
    """Fetch every page of orders for both statuses, print them, and save them locally."""

    print("=" * 60)
    print("TikTok Shop - Get Orders")
    print("=" * 60)
    print(f"App Key: {config.TIKTOKSHOP_APP_KEY}")
    print(f"Shop ID: {config.TIKTOKSHOP_SHOP_ID}")
    print(f"Open API Base URL: {TIKTOKSHOP_OPEN_API_BASE_URL}")
    print(f"Target statuses: {', '.join(TARGET_STATUSES)}")
    print()

    # STEP 1: Load the access token from the tokens file written by bootstrap.
    access_token = _load_access_token()

    # STEP 2: Read shop_cipher from environment, or prompt once if missing.
    shop_cipher = os.environ.get("TIKTOKSHOP_SHOP_CIPHER", "").strip()
    if not shop_cipher:
        shop_cipher = input("Paste your TikTok Shop shop_cipher: ").strip()

    if not shop_cipher:
        print("No shop_cipher provided. Aborting.")
        sys.exit(1)

    # STEP 3: Fetch all pages for each target status separately.
    # We do two independent searches because order_status is treated as a
    # single-value filter in this request shape.
    merged_orders_by_id = {}

    for status in TARGET_STATUSES:
        print("\n" + "-" * 60)
        print(f"Fetching status: {status}")
        print("-" * 60)

        status_orders = _fetch_all_pages_for_status(
            access_token=access_token,
            shop_cipher=shop_cipher,
            status=status,
            page_size=DEFAULT_PAGE_SIZE,
        )

        print(f"Total fetched for {status}: {len(status_orders)}")

        # STEP 4: Merge and de-duplicate by order_id.
        for order in status_orders:
            order_id = _get_order_id(order)

            # If TikTok returns an order without a recognizable id,
            # keep it using a synthetic key so we do not lose data.
            if not order_id:
                order_id = f"unknown_{len(merged_orders_by_id) + 1}"

            merged_orders_by_id[order_id] = order

    all_orders = list(merged_orders_by_id.values())

    # STEP 5: Save the combined result for inspection and later use.
    OUTPUT_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(all_orders, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print(f"Done. Total merged orders fetched: {len(all_orders)}")
    print(f"Saved to: {OUTPUT_FILE_PATH}")
    print("=" * 60)

    _print_orders_list(all_orders)


def _fetch_all_pages_for_status(access_token, shop_cipher, status, page_size):
    """
    Fetches all pages for one specific order status.

    Returns:
      A flat list of orders.
    """
    all_orders = []
    page_token = None
    page_number = 1

    while True:
        print(f"\nFetching page {page_number} for status {status}...")
        page_result = _fetch_orders_page(
            access_token=access_token,
            shop_cipher=shop_cipher,
            status=status,
            page_size=page_size,
            page_token=page_token,
        )

        orders = page_result["orders"]
        next_page_token = page_result["next_page_token"]
        has_more = page_result["has_more"]

        print(f"  Orders returned on this page: {len(orders)}")
        all_orders.extend(orders)

        # STEP 1: Stop when there is no next page.
        if not has_more or not next_page_token:
            break

        page_token = next_page_token
        page_number += 1

        # STEP 2: Small pause to be polite to the API.
        time.sleep(0.3)

    return all_orders


def _load_access_token():
    """
    Reads the access_token from data/tiktokshop_tokens.json.
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


def _fetch_orders_page(access_token, shop_cipher, status, page_size, page_token=None):
    """
    Fetches one page of orders for one status.

    Returns a dict:
      {
        "orders": [...],
        "next_page_token": "... or None",
        "has_more": True/False
      }
    """

    # STEP 1: Use the provided status as a single filter value.
    body = {
        "order_status": status
    }
    body_string = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    # STEP 2: Build query params to match the validated curl shape.
    timestamp = int(time.time())
    query_params = {
        "access_token": access_token,
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "page_size": str(page_size),
        "shop_cipher": shop_cipher,
        "shop_id": str(config.TIKTOKSHOP_SHOP_ID),
        "timestamp": str(timestamp),
        "version": "202309",
    }

    if page_token:
        query_params["page_token"] = page_token

    # STEP 3: Create the signature using the exact same body string
    # that we will send on the wire.
    sign = _make_signature(
        path=ORDER_SEARCH_PATH,
        query_params=query_params,
        body_string=body_string,
        app_secret=config.TIKTOKSHOP_APP_SECRET,
    )
    query_params["sign"] = sign

    # STEP 4: Build the request URL and headers.
    url = f"{TIKTOKSHOP_OPEN_API_BASE_URL}{ORDER_SEARCH_PATH}"
    headers = {
        "x-tts-access-token": access_token,
        "content-type": "application/json",
    }

    # STEP 5: Send the request using data=body_string so the sent body
    # exactly matches the signed body.
    response = requests.post(
        url,
        params=query_params,
        headers=headers,
        data=body_string.encode("utf-8"),
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
            f"TikTok Shop HTTP error. "
            f"Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}"
        )

    api_code = data.get("code")
    if api_code not in (0, "0", None):
        raise RuntimeError(
            f"TikTok Shop API error. "
            f"Code={api_code}, Body={json.dumps(data, ensure_ascii=False)}"
        )

    # STEP 7: Extract the payload.
    payload = data.get("data", {})

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


def _make_signature(path, query_params, body_string, app_secret):
    """
    Builds the TikTok Shop request signature.

    Verified against the validated curl shape:
      1. Exclude 'sign' and 'access_token' from query params.
      2. Sort remaining query params by key.
      3. Concatenate as key + value.
      4. Prepend the request path.
      5. Append the exact raw JSON body string.
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

    # STEP 3: Build the exact string-to-sign.
    string_to_sign = f"{path}{param_string}{body_string}"
    wrapped_string = f"{app_secret}{string_to_sign}{app_secret}"

    # STEP 4: HMAC-SHA256 -> lowercase hex.
    signature = hmac.new(
        app_secret.encode("utf-8"),
        wrapped_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return signature


def _get_order_id(order):
    """
    Returns the order id using the common field names TikTok may return.
    """
    return (
            order.get("id")
            or order.get("order_id")
            or order.get("orderId")
    )


def _print_orders_list(orders):
    """
    Prints a readable list of orders to the terminal.
    """

    # STEP 1: Handle the empty case clearly.
    if not orders:
        print("\nNo matching orders found.")
        return

    print("\nOrders list:")
    print("-" * 60)

    # STEP 2: Print one concise line per order.
    for index, order in enumerate(orders, start=1):
        order_id = _get_order_id(order) or "-"

        status = (
                order.get("order_status")
                or order.get("status")
                or order.get("orderStatus")
                or "-"
        )

        create_time = (
                order.get("create_time")
                or order.get("createTime")
                or order.get("create_timestamp")
                or "-"
        )

        buyer_name = (
                order.get("buyer_name")
                or order.get("buyerName")
                or order.get("recipient_name")
                or "-"
        )

        print(
            f"{index}. "
            f"order_id={order_id} | "
            f"status={status} | "
            f"create_time={create_time} | "
            f"buyer={buyer_name}"
        )


if __name__ == "__main__":
    main()