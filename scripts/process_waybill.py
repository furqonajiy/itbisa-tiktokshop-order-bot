"""
process_waybill.py
------------------
Generates TikTok Shop package waybills by calling the new
Get Package Shipping Document API, then downloads the returned PDF,
converts it to images, and sends them to Telegram.

What this script does:
  1. Loads the access_token from data/tiktokshop_tokens.json.
  2. Reads shop_cipher from environment, or prompts once if missing.
  3. Loads data/ready_to_ship_orders.json written by get_ready_to_ship_orders.py.
  4. Extracts package ids from that file.
  5. For each package id:
     - Calls Get Package Shipping Document (New).
     - Reads the returned doc_url.
     - Downloads the PDF from doc_url.
     - Converts the PDF to PNG image(s).
     - Sends the image(s) to Telegram.
  6. Saves the original PDF locally to data/waybills/.

Important:
  - This script defaults document_type to PACKING_SLIP because that matches
    the curl example you provided.
  - It reuses src.label_processor and src.telegram_sender from this repo.
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import requests

# Add project root to path so we can import src modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config, label_processor, telegram_sender


# STEP 0: Constants used only by this script.
TIKTOKSHOP_OPEN_API_BASE_URL = "https://open-api.tiktokglobalshop.com"
READY_TO_SHIP_ORDERS_FILE_PATH = PROJECT_ROOT / "data" / "ready_to_ship_orders.json"
TOKENS_FILE_PATH = PROJECT_ROOT / "data" / "tiktokshop_tokens.json"
OUTPUT_WAYBILLS_DIR = PROJECT_ROOT / "data" / "waybills"

DOCUMENT_TYPE = "PACKING_SLIP"
API_VERSION = "202309"


def main():
    """Entry point for the helper script."""

    print("=" * 60)
    print("TikTok Shop - Process Package Waybill")
    print("=" * 60)
    print(f"App Key: {config.TIKTOKSHOP_APP_KEY}")
    print(f"Shop ID: {config.TIKTOKSHOP_SHOP_ID}")
    print(f"Open API Base URL: {TIKTOKSHOP_OPEN_API_BASE_URL}")
    print(f"Document Type: {DOCUMENT_TYPE}")
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

    # STEP 3: Load the orders file created by get_ready_to_ship_orders.py.
    orders = _load_orders_file()

    # STEP 4: Extract package ids from the orders file.
    package_jobs = _extract_package_jobs(orders)

    print(f"Orders found in file: {len(orders)}")
    print(f"Unique package ids found: {len(package_jobs)}")
    print()

    if not package_jobs:
        print("No package ids found in data/ready_to_ship_orders.json.")
        print("Please check the file structure returned by get_ready_to_ship_orders.py.")
        sys.exit(1)

    # STEP 5: Make sure the output folder exists.
    OUTPUT_WAYBILLS_DIR.mkdir(parents=True, exist_ok=True)

    # STEP 6: Process each package id one by one.
    success_count = 0
    failed_packages = []

    for index, package_job in enumerate(package_jobs, start=1):
        package_id = package_job["package_id"]
        source_order = package_job["order"]

        print("-" * 60)
        print(f"[{index}/{len(package_jobs)}] Processing package_id={package_id}")

        try:
            # STEP 6a: Call Get Package Shipping Document (New).
            response_json = _get_package_shipping_document(
                access_token=access_token,
                shop_cipher=shop_cipher,
                package_id=package_id,
                document_type=DOCUMENT_TYPE,
            )

            # STEP 6b: Read doc_url from the response.
            doc_url = response_json.get("data", {}).get("doc_url", "").strip()
            if not doc_url:
                raise RuntimeError(
                    f"No doc_url returned for package_id={package_id}. "
                    f"Response={json.dumps(response_json, ensure_ascii=False)}"
                )

            print("  ✓ Received doc_url")

            # STEP 6c: Download the PDF from doc_url.
            pdf_bytes = _download_pdf_from_doc_url(doc_url)
            print(f"  ✓ Downloaded PDF ({len(pdf_bytes)} bytes)")

            # STEP 6d: Save the raw PDF locally.
            pdf_output_path = OUTPUT_WAYBILLS_DIR / f"{package_id}.pdf"
            with open(pdf_output_path, "wb") as f:
                f.write(pdf_bytes)
            print(f"  ✓ Saved PDF to: {pdf_output_path}")

            # STEP 6e: Convert the PDF to PNG image(s).
            png_pages = label_processor.pdf_to_pngs(pdf_bytes)
            print(f"  ✓ Converted PDF to {len(png_pages)} PNG page(s)")

            # STEP 6f: Build a Telegram caption.
            caption = _build_caption(
                order=source_order,
                package_id=package_id,
                document_type=DOCUMENT_TYPE,
            )

            # STEP 6g: Send the image(s) to Telegram.
            delivered = telegram_sender.send_label(png_pages, caption)
            if not delivered:
                raise RuntimeError("Telegram delivery failed.")

            print("  ✓ Sent to Telegram")
            success_count += 1

        except Exception as e:
            print(f"  ✗ Failed: {e}")
            failed_packages.append((package_id, str(e)))

        # STEP 6h: Small pause between packages to be polite to the API.
        time.sleep(0.5)

    # STEP 7: Send a final summary to Telegram.
    summary_text = _build_summary_message(
        success_count=success_count,
        failed_packages=failed_packages,
    )
    telegram_sender.send_summary(summary_text)

    # STEP 8: Print final summary.
    print()
    print("=" * 60)
    print("Done")
    print("=" * 60)
    print(f"Successful packages: {success_count}")
    print(f"Failed packages: {len(failed_packages)}")

    if failed_packages:
        print("\nFailed packages:")
        for package_id, error_message in failed_packages:
            print(f"  - {package_id}: {error_message}")


def _load_access_token():
    """
    Reads the access_token from data/tiktokshop_tokens.json.

    Supports both:
      1. {"access_token": "..."}
      2. {"data": {"access_token": "..."}}
    """

    # STEP 1: Make sure the tokens file exists.
    if not TOKENS_FILE_PATH.exists():
        raise FileNotFoundError(
            f"Tokens file not found at {TOKENS_FILE_PATH}. "
            f"Run scripts/bootstrap_tokens.py first."
        )

    # STEP 2: Parse the JSON file.
    with open(TOKENS_FILE_PATH, "r", encoding="utf-8") as f:
        tokens = json.load(f)

    # STEP 3: Support both flat and nested token structures.
    access_token = str(tokens.get("access_token", "")).strip()
    if access_token:
        return access_token

    nested_access_token = str(tokens.get("data", {}).get("access_token", "")).strip()
    if nested_access_token:
        return nested_access_token

    raise RuntimeError(
        f"No access_token found in {TOKENS_FILE_PATH}. "
        f"Re-run bootstrap if needed."
    )


def _load_orders_file():
    """
    Loads the orders file written by get_ready_to_ship_orders.py.

    Returns:
      A list of order dicts.
    """

    # STEP 1: Make sure the file exists.
    if not READY_TO_SHIP_ORDERS_FILE_PATH.exists():
        raise FileNotFoundError(
            f"Orders file not found at {READY_TO_SHIP_ORDERS_FILE_PATH}. "
            f"Run scripts/get_ready_to_ship_orders.py first."
        )

    # STEP 2: Parse the JSON file.
    with open(READY_TO_SHIP_ORDERS_FILE_PATH, "r", encoding="utf-8") as f:
        orders = json.load(f)

    # STEP 3: Validate that it is a list.
    if not isinstance(orders, list):
        raise RuntimeError(
            f"Expected a list in {READY_TO_SHIP_ORDERS_FILE_PATH}, "
            f"but found {type(orders).__name__}."
        )

    return orders


def _extract_package_jobs(orders):
    """
    Extracts unique package ids from the orders list.

    Why this helper exists:
      TikTok Shop responses can nest package ids in different places depending
      on endpoint version and shop setup. Instead of assuming one exact shape,
      we scan the structure recursively for common package id field names.

    Returns:
      A list of dicts like:
        [
          {
            "package_id": "123",
            "order": {...first order where it was seen...}
          }
        ]
    """

    # STEP 1: Walk every order and collect package ids.
    package_id_to_order = {}

    for order in orders:
        package_ids = sorted(_find_package_ids_recursive(order))

        for package_id in package_ids:
            # Keep the first matching order as the source object for caption building.
            if package_id not in package_id_to_order:
                package_id_to_order[package_id] = order

    # STEP 2: Convert to a stable list structure.
    package_jobs = []
    for package_id in sorted(package_id_to_order.keys()):
        package_jobs.append({
            "package_id": package_id,
            "order": package_id_to_order[package_id],
        })

    return package_jobs


def _find_package_ids_recursive(value):
    """
    Recursively scans a nested structure and returns a set of package ids.

    We look for common field names like:
      - package_id
      - packageId
      - package_ids
      - packageIds
    """

    found = set()

    # STEP 1: Handle dictionaries.
    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = str(key)

            # Direct single package id fields.
            if normalized_key in ("package_id", "packageId"):
                if nested_value not in (None, ""):
                    found.add(str(nested_value))

            # Direct list-of-package-id fields.
            if normalized_key in ("package_ids", "packageIds"):
                if isinstance(nested_value, list):
                    for item in nested_value:
                        if item not in (None, ""):
                            found.add(str(item))

            # Continue recursive scan for nested objects/lists.
            found.update(_find_package_ids_recursive(nested_value))

    # STEP 2: Handle lists.
    elif isinstance(value, list):
        for item in value:
            found.update(_find_package_ids_recursive(item))

    return found


def _get_package_shipping_document(access_token, shop_cipher, package_id, document_type):
    """
    Calls Get Package Shipping Document (New) for one package id.

    This follows the request shape from the user's confirmed curl example:
      GET /fulfillment/202309/packages/{package_id}/shipping_documents
    """

    # STEP 1: Build the path with the package id.
    path = f"/fulfillment/{API_VERSION}/packages/{package_id}/shipping_documents"

    # STEP 2: Build the signed query params.
    timestamp = int(time.time())
    query_params = {
        "access_token": access_token,
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "document_type": document_type,
        "shop_cipher": shop_cipher,
        "shop_id": str(config.TIKTOKSHOP_SHOP_ID),
        "timestamp": str(timestamp),
        "version": API_VERSION,
    }

    sign = _make_signature(
        path=path,
        query_params=query_params,
        body_string="",
        app_secret=config.TIKTOKSHOP_APP_SECRET,
    )
    query_params["sign"] = sign

    # STEP 3: Build URL and headers.
    url = f"{TIKTOKSHOP_OPEN_API_BASE_URL}{path}"
    headers = {
        "x-tts-access-token": access_token,
    }

    # STEP 4: Send the request.
    response = requests.get(
        url,
        params=query_params,
        headers=headers,
        timeout=30,
    )

    # STEP 5: Parse and validate response.
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Get Package Shipping Document did not return valid JSON. "
            f"Status={response.status_code}, Body={response.text}"
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Get Package Shipping Document HTTP error. "
            f"Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}"
        )

    api_code = data.get("code")
    if api_code not in (0, "0", None):
        raise RuntimeError(
            f"Get Package Shipping Document API error. "
            f"Code={api_code}, Body={json.dumps(data, ensure_ascii=False)}"
        )

    return data


def _download_pdf_from_doc_url(doc_url):
    """
    Downloads the PDF file from the doc_url returned by TikTok Shop.

    The doc_url is already a signed file URL, so this second request does not
    need TikTok Shop API signing.
    """

    # STEP 1: Request the file URL.
    response = requests.get(doc_url, timeout=60)

    # STEP 2: Check for a successful HTTP status.
    if response.status_code != 200:
        raise RuntimeError(
            f"doc_url download failed. "
            f"Status={response.status_code}, Body preview={response.text[:500]}"
        )

    # STEP 3: Confirm the response looks like a PDF.
    content_type = response.headers.get("content-type", "").lower()
    if "application/pdf" in content_type:
        return response.content

    # STEP 4: Some file servers may omit or vary content-type. Check file signature.
    if response.content.startswith(b"%PDF"):
        return response.content

    raise RuntimeError(
        f"doc_url did not return a PDF. "
        f"Content-Type={response.headers.get('content-type')}, "
        f"Body preview={response.text[:500]}"
    )


def _make_signature(path, query_params, body_string, app_secret):
    """
    Builds the TikTok Shop request signature.

    This matches the same validated signing style already used in
    get_ready_to_ship_orders.py:
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


def _build_caption(order, package_id, document_type):
    """
    Builds a simple Telegram caption for one package waybill.

    We keep the caption short so it is easy for warehouse staff to read.
    """

    # STEP 1: Pull out the order id if available.
    order_id = (
            order.get("id")
            or order.get("order_id")
            or order.get("orderId")
            or "?"
    )

    # STEP 2: Pull out a readable status if available.
    status = (
            order.get("order_status")
            or order.get("status")
            or order.get("orderStatus")
            or "-"
    )

    # STEP 3: Try to summarize item lines from common response shapes.
    item_lines = _extract_item_lines(order)
    items_text = "\n".join(item_lines) if item_lines else "  (detail barang tidak tersedia)"

    # STEP 4: Assemble the caption in Bahasa Indonesia.
    caption = (
        f"📦 Order: {order_id}\n"
        f"📮 Package: {package_id}\n"
        f"🧾 Document: {document_type}\n"
        f"📌 Status: {status}\n"
        f"\n"
        f"Barang:\n"
        f"{items_text}"
    )
    return caption


def _extract_item_lines(order):
    """
    Tries to build readable item lines from several common TikTok Shop shapes.

    This is intentionally defensive because the exact field names can differ
    between endpoints and versions.
    """

    # STEP 1: Look for the most common item containers.
    items = (
            order.get("item_list")
            or order.get("line_items")
            or order.get("lineItems")
            or order.get("sku_list")
            or []
    )

    if not isinstance(items, list):
        return []

    lines = []

    # STEP 2: Build one line per item.
    for item in items:
        if not isinstance(item, dict):
            continue

        qty = (
                item.get("quantity")
                or item.get("model_quantity_purchased")
                or item.get("count")
                or 1
        )

        sku = (
                (item.get("model_sku") or "").strip()
                or (item.get("item_sku") or "").strip()
                or (item.get("seller_sku") or "").strip()
                or (item.get("sku_id") or "").strip()
                or item.get("product_name")
                or item.get("item_name")
                or "(tidak ada nama)"
        )

        lines.append(f"  • {qty} x {sku}")

    return lines


def _build_summary_message(success_count, failed_packages):
    """
    Builds the final Telegram summary message.
    """

    # STEP 1: No failures.
    if not failed_packages:
        return f"✅ Waybill selesai dikirim. Berhasil: {success_count}, gagal: 0"

    # STEP 2: Include a short failure list when some packages failed.
    failed_list = ", ".join(package_id for package_id, _ in failed_packages[:10])
    return (
        f"⚠️ Waybill selesai diproses. "
        f"Berhasil: {success_count}, gagal: {len(failed_packages)}.\n"
        f"Package gagal: {failed_list}"
    )


if __name__ == "__main__":
    main()