"""
tiktokshop_client.py
--------------------
Talks to the TikTok Shop Open API.

Every Open API call must be signed (HMAC-SHA256) and include a standard set
of query params plus an x-tts-access-token header. All of that is hidden
behind _call_signed so the rest of the code just sees plain functions.

Public functions (used by main.py):
  - get_pending_orders() -> list of order dicts
  - ship_packages(package_ids) -> None
  - get_waybill_pdf(package_id) -> bytes, or None if not ready yet

We commit to TikTok Shop's documented response shape. If the shape ever
changes, these functions will raise KeyError loudly and we will fix it
at that time.
"""

import hashlib
import hmac
import json
import time

import requests

from src import config
from src import tiktokshop_auth

_PENDING_STATUSES = ("AWAITING_SHIPMENT", "AWAITING_COLLECTION")

# Shop cipher is stable for the authorization lifetime, so one fetch per
# process run is enough. Cached here.
_cached_shop_cipher = None


# ============================================================
# Public function 1: get_pending_orders
# ============================================================

def get_pending_orders():
    """Returns all orders in AWAITING_SHIPMENT or AWAITING_COLLECTION.

    TikTok Shop's order search endpoint only accepts one status per call,
    so we run two searches and merge by order id. AWAITING_SHIPMENT is
    checked first so an order that somehow appears in both stays tagged
    as still needing shipment.
    """
    merged = {}

    for status in _PENDING_STATUSES:
        print(f"  Searching orders with status {status}...")
        status_orders = _search_orders_by_status(status)
        print(f"  Status {status}: {len(status_orders)} order(s)")

        for order in status_orders:
            order_id = order["id"]
            if order_id not in merged:
                merged[order_id] = order

    return list(merged.values())


def _search_orders_by_status(status):
    """Paginates through all orders for one status."""
    all_orders = []
    page_token = None

    while True:
        body = {"order_status": status}
        extra_query = {"page_size": "50"}
        if page_token:
            extra_query["page_token"] = page_token

        response = _call_signed(
            "POST",
            "/order/202309/orders/search",
            extra_query=extra_query,
            body=body,
        )
        _check_ok(response, context=f"order search ({status})")

        payload = response.json()["data"]
        all_orders.extend(payload["orders"])

        # TikTok returns an empty string on the last page.
        page_token = payload["next_page_token"]
        if not page_token:
            break

        time.sleep(0.3)  # Be polite between pages.

    return all_orders


# ============================================================
# Public function 2: ship_packages
# ============================================================

def ship_packages(package_ids):
    """Marks the given packages as ready to ship in one batch call.

    Partial failures (per-package errors inside the response body) are not
    treated as hard errors here. Any package whose shipment didn't actually
    land will simply return "not ready" when we ask for its waybill, and
    the next scheduled run will retry.
    """
    if not package_ids:
        return

    body = {"packages": [{"id": pid} for pid in package_ids]}
    response = _call_signed("POST", "/fulfillment/202309/packages/ship", body=body)
    _check_ok(response, context="batch ship packages")


# ============================================================
# Public function 3: get_waybill_pdf
# ============================================================

def get_waybill_pdf(package_id):
    """Generates and downloads the waybill PDF for one package.

    Two steps:
      1. Ask TikTok Shop for a signed doc_url for the PDF.
      2. Download the PDF from doc_url (no TikTok signing needed; doc_url
         is already pre-signed).

    Returns None if the PDF is still being rendered, so main.py can retry
    on the next scheduled run.
    """
    path = f"/fulfillment/202309/packages/{package_id}/shipping_documents"
    extra_query = {"document_type": config.TIKTOKSHOP_DOCUMENT_TYPE}

    # Retry a few times within this run since TikTok sometimes needs a
    # moment after batch-ship to finish rendering.
    for attempt in range(3):
        if attempt > 0:
            time.sleep(5)

        response = _call_signed("GET", path, extra_query=extra_query)
        data = response.json()

        if data["code"] != 0:
            print(f"  Waybill for {package_id} not ready (attempt {attempt + 1}/3): {data.get('message')}")
            continue

        doc_url = data["data"]["doc_url"].strip()
        if not doc_url:
            print(f"  Waybill for {package_id} returned empty doc_url (attempt {attempt + 1}/3)")
            continue

        pdf_bytes = _download_pdf(doc_url)
        if pdf_bytes is None:
            print(f"  Waybill PDF for {package_id} did not download cleanly (attempt {attempt + 1}/3)")
            continue

        return pdf_bytes

    print(f"  Waybill for {package_id} still not ready, will retry next run")
    return None


def _download_pdf(doc_url):
    """Downloads a PDF from a pre-signed doc_url."""
    response = requests.get(doc_url, timeout=60)
    if response.status_code != 200:
        return None

    # Content-type is usually application/pdf but can be missing; fall back
    # to checking the file signature.
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/pdf" in content_type or response.content.startswith(b"%PDF"):
        return response.content

    return None


# ============================================================
# Internal: shop cipher (fetched once per run)
# ============================================================

def _get_shop_cipher():
    """Returns the shop cipher, fetching it once and caching for the run.

    Most Open API endpoints require shop_cipher as a query param. The cipher
    itself comes from /authorization/202309/shops — the one endpoint that
    does NOT require shop_cipher (chicken-and-egg).
    """
    global _cached_shop_cipher
    if _cached_shop_cipher:
        return _cached_shop_cipher

    response = _call_signed("GET", "/authorization/202309/shops", include_cipher=False)
    _check_ok(response, context="authorized shops")

    shops = response.json()["data"]["shops"]
    matching = next((s for s in shops if s["id"] == config.TIKTOKSHOP_SHOP_ID), None)
    if matching is None:
        raise RuntimeError(
            f"Shop {config.TIKTOKSHOP_SHOP_ID} not found in authorized shops. "
            f"Found: {[s['id'] for s in shops]}"
        )

    _cached_shop_cipher = matching["cipher"]
    print(f"  Fetched shop cipher for shop {config.TIKTOKSHOP_SHOP_ID}")
    return _cached_shop_cipher


# ============================================================
# Internal: signed HTTP transport
# ============================================================

def _call_signed(method, path, extra_query=None, body=None, include_cipher=True):
    """Sends one signed request. Returns the raw requests.Response.

    Callers use _check_ok to assert success and then read .json() or
    .content themselves.
    """
    access_token = tiktokshop_auth.get_valid_access_token()

    query_params = {
        "access_token": access_token,
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "shop_id": config.TIKTOKSHOP_SHOP_ID,
        "timestamp": str(int(time.time())),
        "version": "202309",
    }
    if include_cipher:
        query_params["shop_cipher"] = _get_shop_cipher()
    if extra_query:
        query_params.update(extra_query)

    # The body must be signed and sent byte-for-byte identically.
    body_string = ""
    if body is not None:
        body_string = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    query_params["sign"] = _make_signature(path, query_params, body_string)

    url = f"{config.TIKTOKSHOP_OPEN_API_BASE_URL}{path}"
    headers = {"x-tts-access-token": access_token}

    if method == "GET":
        return requests.get(url, params=query_params, headers=headers, timeout=30)

    headers["content-type"] = "application/json"
    return requests.post(
        url,
        params=query_params,
        headers=headers,
        data=body_string.encode("utf-8"),
        timeout=60,
    )


def _make_signature(path, query_params, body_string):
    """HMAC-SHA256 signature per TikTok Shop's signing algorithm.

    Steps:
      1. Exclude 'sign' and 'access_token' from the signed params, plus any
         empty values.
      2. Sort remaining params by key, concatenate as key+value.
      3. Canonical = path + sorted_params + raw_body_string.
      4. Wrap with app_secret at both ends.
      5. HMAC-SHA256 with app_secret, lowercase hex.
    """
    app_secret = config.TIKTOKSHOP_APP_SECRET

    filtered = {
        k: str(v) for k, v in query_params.items()
        if k not in ("sign", "access_token") and v not in (None, "")
    }
    param_string = "".join(f"{k}{filtered[k]}" for k in sorted(filtered))
    wrapped = f"{app_secret}{path}{param_string}{body_string}{app_secret}"

    return hmac.new(
        app_secret.encode("utf-8"),
        wrapped.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _check_ok(response, context):
    """Raises RuntimeError if the response is not a TikTok success."""
    if response.status_code != 200:
        raise RuntimeError(f"{context} HTTP {response.status_code}: {response.text}")

    data = response.json()
    if data["code"] != 0:
        raise RuntimeError(f"{context} API error: {data.get('message')} (code={data['code']})")