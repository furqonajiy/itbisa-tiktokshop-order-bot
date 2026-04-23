"""
tiktokshop_client.py
--------------------
Talks to the TikTok Shop Open API.

Why this file exists:
  This module isolates all TikTok Shop specific concerns in one place:
    - signed request construction
    - order search
    - order detail lookup
    - mark-ready-to-ship call
    - shipping label download

  The rest of the code does not need to know anything about HMAC signing,
  TikTok Shop endpoints, or response-shape quirks.

Public functions (used by main.py):
  - get_pending_orders() -> list of normalized order dicts
  - ship_order(order_id) -> None (raises on error)
  - get_shipping_label_pdf(order_id) -> bytes (or None if not ready yet)

Fake mode:
  When config.USE_FAKE_TIKTOKSHOP is True, the public functions below delegate
  to tiktokshop_client_fake instead of calling the real API.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlparse

import requests

from src import config


# TikTok Shop statuses we care about for this bot.
# AWAITING_SHIPMENT  -> seller still needs to mark order ready to ship.
# AWAITING_COLLECTION -> shipment arranged, label should be downloadable.
_PENDING_STATUSES = {"AWAITING_SHIPMENT", "AWAITING_COLLECTION"}


# ============================================================
# Internal helpers (start with underscore = "do not use from outside")
# ============================================================

def _compact_json(body):
    """Serializes request body in a stable compact form for signing."""
    if not body:
        return ""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)



def _make_signature(path, params, body=None):
    """
    Builds the HMAC-SHA256 signature for a TikTok Shop request.

    Important note for junior engineers:
      TikTok Shop signs the API path plus selected query parameters and,
      for requests with a body, the compact JSON body. The exact public
      signing guides have moved around over time, so we keep the signing
      logic isolated in one place. If TikTok changes signing rules later,
      this is the only function you should need to update.
    """

    # STEP 1: Remove keys that should never participate in the signature.
    # access_token is sent as a query parameter but excluded from signing in
    # the commonly used TikTok Shop signing flow.
    filtered = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        if key in {"sign", "access_token"}:
            continue
        filtered[str(key)] = str(value)

    # STEP 2: Sort keys alphabetically and concatenate as key+value pairs.
    sorted_pairs = "".join(f"{key}{filtered[key]}" for key in sorted(filtered))

    # STEP 3: Build the canonical string to sign.
    canonical = f"{path}{sorted_pairs}{_compact_json(body)}"

    # STEP 4: Wrap with app secret and sign using HMAC-SHA256.
    wrapped = f"{config.TIKTOKSHOP_APP_SECRET}{canonical}{config.TIKTOKSHOP_APP_SECRET}"
    signature = hmac.new(
        config.TIKTOKSHOP_APP_SECRET.encode("utf-8"),
        wrapped.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return signature



def _build_signed_request(path, body=None, extra_params=None):
    """
    Builds the full request URL and signed query params for one API call.
    """

    # STEP 1: Get a fresh access token. We import inside the function to avoid
    # a circular import at module load time.
    from src import tiktokshop_auth
    access_token = tiktokshop_auth.get_valid_access_token()

    # STEP 2: Start from TikTok Shop's common query params.
    params = {
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "access_token": access_token,
        "shop_id": config.TIKTOKSHOP_SHOP_ID,
        "timestamp": int(time.time()),
    }

    # STEP 3: Add endpoint-specific query params when needed.
    if extra_params:
        params.update(extra_params)

    # STEP 4: Generate the signature.
    params["sign"] = _make_signature(path=path, params=params, body=body)

    # STEP 5: Build the full URL.
    url = f"{config.TIKTOKSHOP_AUTH_BASE_URL}{path}"
    return url, params



def _call_get(path, extra_params=None):
    """Signed GET returning raw requests.Response."""
    url, params = _build_signed_request(path=path, body=None, extra_params=extra_params)
    return requests.get(url, params=params, timeout=30)



def _call_post(path, body):
    """Signed POST returning raw requests.Response."""
    url, params = _build_signed_request(path=path, body=body)
    return requests.post(url, params=params, json=body, timeout=30)



def _unwrap_payload(data):
    """Normalizes TikTok Shop responses into the useful payload object."""
    if isinstance(data.get("data"), dict):
        return data["data"]
    if isinstance(data.get("response"), dict):
        return data["response"]
    return data



def _has_api_error(response, data):
    """Returns True if the HTTP or JSON response indicates an API error."""
    if response.status_code != 200:
        return True
    code = data.get("code")
    if code in (None, 0, "0"):
        return False
    return True



def _extract_error_message(response, data):
    """Pulls the most useful error message we can find."""
    return (
        data.get("message")
        or data.get("msg")
        or data.get("error")
        or data.get("error_message")
        or response.text
    )



def _normalize_order_id(value):
    """Converts an order id to a clean string for internal use."""
    if value is None:
        return ""
    return str(value)


# ============================================================
# Public functions (used by main.py)
# ============================================================

def get_pending_orders():
    """
    Fetches TikTok Shop orders that need a label printed.

    We intentionally keep the downstream order shape close to the existing bot
    so that main.py, telegram_sender.py, and state_manager.py stay simple.

    Returns:
      A list of normalized order dicts with these fields:
        - order_id
        - order_status
        - shipping_carrier
        - item_list
    """

    # STEP 0: In fake mode, delegate to the fake client and return early.
    if config.USE_FAKE_TIKTOKSHOP:
        from src import tiktokshop_client_fake
        return tiktokshop_client_fake.get_pending_orders()

    # STEP 1: Search recent orders from TikTok Shop.
    summaries = _search_recent_orders()
    if not summaries:
        return []

    # STEP 2: Extract unique order ids.
    order_ids = []
    seen = set()
    for summary in summaries:
        order_id = _normalize_order_id(
            summary.get("order_id")
            or summary.get("id")
            or summary.get("order_sn")
        )
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        order_ids.append(order_id)

    if not order_ids:
        return []

    # STEP 3: Fetch the order details in batches.
    details = _get_order_details(order_ids)

    # STEP 4: Normalize the details into the internal shape used by the bot.
    normalized = []
    for detail in details:
        normalized_order = _normalize_order_detail(detail)
        if not normalized_order:
            continue
        if normalized_order["order_status"] in _PENDING_STATUSES:
            normalized.append(normalized_order)

    return normalized



def ship_order(order_id):
    """
    Marks a TikTok Shop order as ready to ship.

    This is the closest TikTok Shop equivalent to the existing Shopee
    "arrange shipment" step. After this succeeds, the order should move from
    AWAITING_SHIPMENT to AWAITING_COLLECTION and the shipping label should
    become available shortly after.
    """

    # STEP 0: In fake mode, delegate to the fake client.
    if config.USE_FAKE_TIKTOKSHOP:
        from src import tiktokshop_client_fake
        return tiktokshop_client_fake.ship_order(order_id)

    # STEP 1: Call the TikTok Shop ready-to-ship endpoint.
    path = "/api/orders/rts"
    body = {"order_id": str(order_id)}
    response = _call_post(path, body)
    data = _safe_json(response)

    # STEP 2: Raise a helpful error if TikTok Shop rejected the request.
    if _has_api_error(response, data):
        raise RuntimeError(_extract_error_message(response, data))



def get_shipping_label_pdf(order_id):
    """
    Fetches the shipping label PDF for a single TikTok Shop order.

    The existing bot expects a PDF as bytes, so this function keeps that
    contract and hides any TikTok Shop response quirks behind one function.

    Returns:
      PDF file contents as bytes, OR None if the label is not ready yet.
    """

    # STEP 0: In fake mode, delegate to the fake client and return early.
    if config.USE_FAKE_TIKTOKSHOP:
        from src import tiktokshop_client_fake
        return tiktokshop_client_fake.get_shipping_label_pdf(order_id)

    # STEP 1: Try a few times because the label may take a few seconds to appear
    # right after the order is marked ready to ship.
    for attempt in range(3):
        if attempt > 0:
            time.sleep(5)

        pdf_bytes = _download_shipping_document(order_id)
        if pdf_bytes is not None:
            return pdf_bytes

        print(f"  Label for {order_id} not ready yet, attempt {attempt + 1}/3")

    # STEP 2: Give up for this run. The next scheduled run will try again.
    print(f"  Label for {order_id} still not ready, will retry next run")
    return None


# ============================================================
# More internal helpers (used only by the public functions above)
# ============================================================

def _search_recent_orders():
    """
    Searches recent orders from the last 7 days.

    We intentionally search a recent window and then de-duplicate with the
    state file. That is safer than trying to rely on only the newest orders,
    because retries and delayed labels can happen.
    """

    path = "/api/orders/search"
    seven_days_ago = int(time.time()) - (7 * 24 * 60 * 60)
    now = int(time.time())

    all_orders = []
    page_token = None

    while True:
        # STEP 1: Build the request body using the public TikTok Shop order
        # search shape. We search by both create_time and update_time so that
        # delayed status changes still show up in the window.
        body = {
            "create_time_from": seven_days_ago,
            "create_time_to": now,
            "update_time_from": seven_days_ago,
            "update_time_to": now,
            "sort_type": 1,
            "sort_by": "CREATE_TIME",
            "page_size": 100,
        }
        if page_token:
            body["page_token"] = page_token

        # STEP 2: Make the call.
        response = _call_post(path, body)
        data = _safe_json(response)
        if _has_api_error(response, data):
            raise RuntimeError(_extract_error_message(response, data))

        payload = _unwrap_payload(data)

        # STEP 3: Collect the order summaries from whichever key TikTok Shop uses.
        page_orders = (
            payload.get("orders")
            or payload.get("order_list")
            or payload.get("list")
            or payload.get("data")
            or []
        )
        if isinstance(page_orders, list):
            all_orders.extend(page_orders)

        # STEP 4: Continue only when TikTok Shop gives us a next page token.
        page_token = (
            payload.get("next_page_token")
            or payload.get("page_token")
            or payload.get("nextPageToken")
            or payload.get("next_cursor")
        )
        has_more = payload.get("has_more") or payload.get("has_next_page") or False
        if not page_token or not has_more:
            break

    return all_orders



def _get_order_details(order_ids):
    """
    Fetches full details for a list of order ids.

    We do this in batches so very busy shops do not hit payload-size limits.
    """

    path = "/api/orders/detail/query"
    all_details = []

    for batch_start in range(0, len(order_ids), 50):
        batch = order_ids[batch_start:batch_start + 50]
        body = {"order_id_list": batch}

        response = _call_post(path, body)
        data = _safe_json(response)
        if _has_api_error(response, data):
            raise RuntimeError(_extract_error_message(response, data))

        payload = _unwrap_payload(data)
        page_details = (
            payload.get("orders")
            or payload.get("order_list")
            or payload.get("list")
            or payload.get("data")
            or []
        )
        if isinstance(page_details, list):
            all_details.extend(page_details)

    return all_details



def _download_shipping_document(order_id):
    """
    Downloads the shipping document for one order.

    Returns:
      bytes if the PDF is ready, or None if the label is still unavailable.
    """

    # STEP 1: Ask TikTok Shop for the shipping label in A6 size.
    path = "/api/logistics/shipping_document"
    response = _call_get(
        path,
        extra_params={
            "order_id": str(order_id),
            "document_type": "SHIPPING_LABEL",
            "document_size": "A6",
        },
    )

    # STEP 2: If TikTok Shop streamed the PDF directly, return it immediately.
    content_type = response.headers.get("content-type", "")
    if "application/pdf" in content_type:
        return response.content

    # STEP 3: Otherwise try to parse a JSON response.
    data = _safe_json(response)
    if _has_api_error(response, data):
        # "not ready yet" should not fail the whole bot run. We simply retry
        # later in the same run and then again in the next scheduled run.
        return None

    payload = _unwrap_payload(data)

    # STEP 4: Some implementations return a download URL instead of the raw PDF.
    document_url = (
        payload.get("url")
        or payload.get("document_url")
        or payload.get("shipping_document_url")
    )
    if document_url:
        download = requests.get(document_url, timeout=30)
        if download.status_code == 200 and "application/pdf" in download.headers.get("content-type", ""):
            return download.content
        return None

    # STEP 5: No PDF and no URL means the label is not ready yet.
    return None



def _safe_json(response):
    """Returns parsed JSON if possible, else an empty dict."""
    try:
        return response.json()
    except ValueError:
        return {}



def _normalize_order_detail(detail):
    """
    Converts a raw TikTok Shop order detail into the internal shape used by
    the rest of the bot.
    """

    # STEP 1: Pull out the core order fields.
    order_id = _normalize_order_id(
        detail.get("order_id")
        or detail.get("id")
        or detail.get("order_sn")
    )
    if not order_id:
        return None

    order_status = (
        detail.get("status")
        or detail.get("order_status")
        or "UNKNOWN"
    )

    # STEP 2: Try to identify the shipping provider name.
    shipping_carrier = (
        detail.get("shipping_provider")
        or detail.get("shipping_provider_name")
        or detail.get("shipping_carrier")
        or detail.get("delivery_option_name")
        or "?"
    )

    # STEP 3: Normalize line items into the same structure telegram_sender expects.
    raw_items = (
        detail.get("line_items")
        or detail.get("item_list")
        or detail.get("sku_list")
        or detail.get("order_items")
        or []
    )

    item_list = []
    for raw_item in raw_items:
        seller_sku = (
            raw_item.get("seller_sku")
            or raw_item.get("sku")
            or raw_item.get("item_sku")
            or raw_item.get("sku_code")
            or ""
        )
        sku_name = (
            raw_item.get("sku_name")
            or raw_item.get("model_name")
            or raw_item.get("variation_name")
            or ""
        )
        product_name = (
            raw_item.get("product_name")
            or raw_item.get("item_name")
            or raw_item.get("name")
            or seller_sku
            or "(tidak ada nama)"
        )
        quantity = (
            raw_item.get("quantity")
            or raw_item.get("model_quantity_purchased")
            or raw_item.get("sku_quantity")
            or 1
        )

        item_list.append(
            {
                "item_name": product_name,
                "item_sku": seller_sku,
                "model_name": sku_name,
                "model_sku": seller_sku,
                "model_quantity_purchased": quantity,
            }
        )

    return {
        "order_id": order_id,
        "order_status": order_status,
        "shipping_carrier": shipping_carrier,
        "item_list": item_list,
    }
