"""
process_order.py
----------------
Takes package ids derived from data/ready_to_ship_orders.json and calls
TikTok Shop's Batch Ship Packages (New) endpoint.

What this script does:
  1. Loads the access_token from data/tiktokshop_tokens.json.
  2. Reads shop_cipher from environment, or prompts once if missing.
  3. Loads data/ready_to_ship_orders.json written by get_ready_to_ship_orders.py.
  4. Extracts package ids from the file.
  5. Removes duplicates while preserving order.
  6. Calls /fulfillment/202309/packages/ship with all package ids in one request.
  7. Prints the response and saves it to data/process_order_result.json.

Important:
  - This script is intentionally aligned with the signing style already used in
    scripts/get_ready_to_ship_orders.py.
  - It ships PACKAGE ids, not ORDER ids.
  - If your input file only contains order ids and no package ids, the script
    will stop and tell you clearly.
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
BATCH_SHIP_PACKAGES_PATH = "/fulfillment/202309/packages/ship"

INPUT_FILE_PATH = PROJECT_ROOT / "data" / "ready_to_ship_orders.json"
OUTPUT_FILE_PATH = PROJECT_ROOT / "data" / "process_order_result.json"


def main():
    """Loads package ids from file and ships them in one batch request."""

    print("=" * 60)
    print("TikTok Shop - Batch Ship Packages")
    print("=" * 60)
    print(f"App Key: {config.TIKTOKSHOP_APP_KEY}")
    print(f"Shop ID: {config.TIKTOKSHOP_SHOP_ID}")
    print(f"Open API Base URL: {TIKTOKSHOP_OPEN_API_BASE_URL}")
    print(f"Input file: {INPUT_FILE_PATH}")
    print()

    # STEP 1: Load the access token from the tokens file.
    access_token = _load_access_token()

    # STEP 2: Read shop_cipher from environment, or prompt once if missing.
    shop_cipher = os.environ.get("TIKTOKSHOP_SHOP_CIPHER", "").strip()
    if not shop_cipher:
        shop_cipher = input("Paste your TikTok Shop shop_cipher: ").strip()

    if not shop_cipher:
        print("No shop_cipher provided. Aborting.")
        sys.exit(1)

    # STEP 3: Load the saved file produced by get_ready_to_ship_orders.py.
    orders_or_packages = _load_input_file()

    # STEP 4: Extract package ids from the saved JSON.
    package_ids = _extract_package_ids(orders_or_packages)

    print(f"Records found in file: {len(orders_or_packages)}")
    print(f"Package ids extracted: {len(package_ids)}")
    print()

    if not package_ids:
        print("No package ids found in input file.")
        print("Make sure data/ready_to_ship_orders.json contains package ids or nested packages.")
        sys.exit(1)

    # STEP 5: Call Batch Ship Packages (New).
    result = _batch_ship_packages(
        access_token=access_token,
        shop_cipher=shop_cipher,
        package_ids=package_ids,
    )

    # STEP 6: Save the full response for debugging / audit.
    OUTPUT_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # STEP 7: Print a clean summary.
    print()
    print("=" * 60)
    print("Batch Ship Packages finished")
    print("=" * 60)
    print(f"Package ids sent: {len(package_ids)}")
    print(f"Saved response to: {OUTPUT_FILE_PATH}")
    print()
    print("Response body:")
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _load_access_token():
    """
    Reads the access_token from data/tiktokshop_tokens.json.

    Supports both:
      1. {"access_token": "..."}
      2. {"data": {"access_token": "..."}}
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

    # STEP 3: Support both flat and nested token structures.
    access_token = str(tokens.get("access_token", "")).strip()
    if access_token:
        return access_token

    nested_access_token = str(tokens.get("data", {}).get("access_token", "")).strip()
    if nested_access_token:
        return nested_access_token

    raise RuntimeError(
        f"No access_token found in {tokens_path}. "
        f"Re-run bootstrap if needed."
    )


def _load_input_file():
    """
    Loads data/ready_to_ship_orders.json.

    Expected shape:
      - usually a list of order dicts
      - but we also support a list of package dicts
      - and even a list of raw package-id strings
    """

    # STEP 1: Make sure the file exists.
    if not INPUT_FILE_PATH.exists():
        raise FileNotFoundError(
            f"Input file not found at {INPUT_FILE_PATH}. "
            f"Run scripts/get_ready_to_ship_orders.py first."
        )

    # STEP 2: Parse JSON.
    with open(INPUT_FILE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # STEP 3: Validate top-level type.
    if not isinstance(data, list):
        raise RuntimeError(
            f"Expected a list in {INPUT_FILE_PATH}, "
            f"but found {type(data).__name__}."
        )

    return data


def _extract_package_ids(records):
    """
    Extracts package ids from the saved file.

    We intentionally DO NOT blindly treat top-level order id as a package id.
    We only extract package-like identifiers from fields that actually look
    like package fields.

    Supported patterns:
      1. raw string items:
         ["1193892791888217574", "1193654760268792961"]

      2. direct package objects:
         [{"package_id":"..."}, {"id":"...", "package_status":"PROCESSING"}]

      3. order objects with nested packages:
         [{"packages":[{"id":"..."}, {"package_id":"..."}]}]

      4. order objects with package_list / packageList:
         [{"package_list":[...]}]

      5. order objects with package_ids / packageIds:
         [{"package_ids":["...", "..."]}]
    """

    # STEP 1: Walk through every record and collect candidate ids.
    package_ids = []

    for record in records:
        # Case A: raw string entry -> treat as package id directly.
        if isinstance(record, str) and record.strip():
            package_ids.append(record.strip())
            continue

        if not isinstance(record, dict):
            continue

        # Case B: direct explicit package_id fields.
        direct_package_id = (
                record.get("package_id")
                or record.get("packageId")
        )
        if direct_package_id:
            package_ids.append(str(direct_package_id).strip())

        # Case C: package_ids array on the record.
        package_ids_array = record.get("package_ids") or record.get("packageIds") or []
        if isinstance(package_ids_array, list):
            for package_id in package_ids_array:
                if package_id:
                    package_ids.append(str(package_id).strip())

        # Case D: nested packages arrays.
        nested_packages_groups = [
            record.get("packages"),
            record.get("package_list"),
            record.get("packageList"),
        ]

        for nested_packages in nested_packages_groups:
            if not isinstance(nested_packages, list):
                continue

            for package in nested_packages:
                if isinstance(package, str) and package.strip():
                    package_ids.append(package.strip())
                    continue

                if not isinstance(package, dict):
                    continue

                package_id = (
                        package.get("package_id")
                        or package.get("packageId")
                        or package.get("id")
                )
                if package_id:
                    package_ids.append(str(package_id).strip())

        # Case E: direct package-shaped object using top-level "id".
        # We only trust top-level "id" when the record also looks package-like.
        if _looks_like_package_record(record):
            package_like_id = record.get("id")
            if package_like_id:
                package_ids.append(str(package_like_id).strip())

    # STEP 2: Remove empty values.
    package_ids = [package_id for package_id in package_ids if package_id]

    # STEP 3: De-duplicate while preserving original order.
    seen = set()
    deduplicated = []
    for package_id in package_ids:
        if package_id in seen:
            continue
        seen.add(package_id)
        deduplicated.append(package_id)

    return deduplicated


def _looks_like_package_record(record):
    """
    Returns True when a dict looks more like a package object than an order object.

    This prevents us from accidentally shipping order ids when the input file
    contains order-level records.
    """
    package_like_keys = {
        "package_status",
        "packageStatus",
        "tracking_number",
        "trackingNumber",
        "shipping_provider",
        "shippingProvider",
        "package_id",
        "packageId",
    }

    return any(key in record for key in package_like_keys)


def _batch_ship_packages(access_token, shop_cipher, package_ids):
    """
    Calls TikTok Shop Batch Ship Packages (New) with all package ids.

    Request body shape:
      {
        "packages": [
          {"id": "1193892791888217574"},
          {"id": "1193654760268792961"}
        ]
      }
    """

    # STEP 1: Build the exact JSON body string that will be signed and sent.
    body = {
        "packages": [{"id": str(package_id)} for package_id in package_ids]
    }
    body_string = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    # STEP 2: Build query params to match the working curl shape.
    timestamp = int(time.time())
    query_params = {
        "access_token": access_token,
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "shop_cipher": shop_cipher,
        "shop_id": str(config.TIKTOKSHOP_SHOP_ID),
        "timestamp": str(timestamp),
        "version": "202309",
    }

    # STEP 3: Create the signature using the exact same body string.
    sign = _make_signature(
        path=BATCH_SHIP_PACKAGES_PATH,
        query_params=query_params,
        body_string=body_string,
        app_secret=config.TIKTOKSHOP_APP_SECRET,
    )
    query_params["sign"] = sign

    # STEP 4: Build the request URL and headers.
    url = f"{TIKTOKSHOP_OPEN_API_BASE_URL}{BATCH_SHIP_PACKAGES_PATH}"
    headers = {
        "x-tts-access-token": access_token,
        "content-type": "application/json",
    }

    print("Calling Batch Ship Packages API...")
    print(f"URL: {url}")
    print(f"Packages count in request: {len(package_ids)}")

    # STEP 5: Send the request using data=body_string so the sent body
    # exactly matches the signed body.
    response = requests.post(
        url,
        params=query_params,
        headers=headers,
        data=body_string.encode("utf-8"),
        timeout=60,
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

    return data


def _make_signature(path, query_params, body_string, app_secret):
    """
    Builds the TikTok Shop request signature.

    This intentionally matches the same signing style already used in
    scripts/get_ready_to_ship_orders.py:
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


if __name__ == "__main__":
    main()