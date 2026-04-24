"""
tiktokshop_client.py
--------------------
Talks to the TikTok Shop Open API.

Why this file exists:
  TikTok Shop requires every Open API call to be signed (HMAC-SHA256 over a
  specific canonical string) and to include several common query parameters
  (app_key, shop_id, shop_cipher, timestamp, version, access_token, sign)
  plus an x-tts-access-token header. The exact signing rules are easy to get
  wrong, so all of it lives here. The rest of the code does not need to know
  anything about signing, base URLs, or TikTok Shop quirks.

The signing logic is copied from scripts/get_ready_to_ship_orders.py and
scripts/process_order.py, which were verified against real curl examples.

Public functions (used by main.py):
  - get_pending_orders() -> list of order dicts (AWAITING_SHIPMENT + AWAITING_COLLECTION)
  - ship_packages(package_ids) -> None (raises on error)
  - get_waybill_pdf(package_id) -> bytes (or None if not ready yet)

Fake mode:
  When config.USE_FAKE_TIKTOKSHOP is True, the public functions below delegate
  to tiktokshop_client_fake so developers can test without real API access.
"""

import hashlib
import hmac
import json
import time

import requests

from src import config


# TikTok Shop statuses we care about for this bot.
#   AWAITING_SHIPMENT   -> seller still needs to mark packages ready to ship.
#   AWAITING_COLLECTION -> shipment arranged, waybill should be downloadable.
_PENDING_STATUSES = ("AWAITING_SHIPMENT", "AWAITING_COLLECTION")


# Shop cipher is fetched once per process run and cached here.
# The cipher is stable for the lifetime of the authorization, so one fetch
# per run is plenty. We still always re-fetch on each run because tokens
# might have been rotated since last run.
_cached_shop_cipher = None


# ============================================================
# Signing (matches the verified scripts exactly)
# ============================================================

def _make_signature(path, query_params, body_string=""):
    """
    Builds the TikTok Shop HMAC-SHA256 signature.

    Algorithm:
      1. Exclude 'sign' and 'access_token' from query params (they are sent
         on the wire but not part of the signed canonical string).
      2. Drop empty values.
      3. Sort remaining query params by key, concatenate as key+value pairs.
      4. Canonical = path + sorted_params + raw_body_string.
      5. Wrap with app_secret at both ends.
      6. HMAC-SHA256 using app_secret, return lowercase hex.
    """

    app_secret = config.TIKTOKSHOP_APP_SECRET

    # STEP 1 + 2: Exclude non-signed keys and empties.
    filtered = {}
    for key, value in query_params.items():
        if key in ("sign", "access_token"):
            continue
        if value is None or value == "":
            continue
        filtered[str(key)] = str(value)

    # STEP 3: Sort and concatenate.
    param_string = "".join(f"{key}{filtered[key]}" for key in sorted(filtered))

    # STEP 4: Canonical string.
    string_to_sign = f"{path}{param_string}{body_string}"

    # STEP 5: Wrap with app_secret.
    wrapped = f"{app_secret}{string_to_sign}{app_secret}"

    # STEP 6: HMAC-SHA256 lowercase hex.
    return hmac.new(
        app_secret.encode("utf-8"),
        wrapped.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ============================================================
# Transport helpers (one place that knows how to actually hit TikTok Shop)
# ============================================================

def _call_signed(method, path, extra_query=None, body=None, include_cipher=True):
    """
    Sends one signed request to the TikTok Shop Open API.

    Args:
      method: "GET" or "POST".
      path: endpoint path, e.g. "/order/202309/orders/search".
      extra_query: additional query params (document_type, page_size, etc.).
      body: JSON-serializable request body for POST requests. None for GET.
      include_cipher: whether to add shop_cipher to the query (and fetch it
        first if not cached). Set False only for the cipher-fetch endpoint
        itself, which must not include shop_cipher.

    Returns:
      The raw requests.Response object. Callers decide how to interpret it
      (JSON, PDF bytes, etc.).
    """

    # STEP 1: Get a fresh access token. Import inline to avoid circular import
    # at module load time.
    from src import tiktokshop_auth
    access_token = tiktokshop_auth.get_valid_access_token()

    # STEP 2: Build the base set of query params.
    query_params = {
        "access_token": access_token,
        "app_key": config.TIKTOKSHOP_APP_KEY,
        "shop_id": str(config.TIKTOKSHOP_SHOP_ID),
        "timestamp": str(int(time.time())),
        "version": config.TIKTOKSHOP_API_VERSION,
    }
    if include_cipher:
        query_params["shop_cipher"] = _get_shop_cipher()
    if extra_query:
        for key, value in extra_query.items():
            if value is None or value == "":
                continue
            query_params[key] = str(value)

    # STEP 3: Compute the exact body string that will be both signed AND sent.
    body_string = ""
    if body is not None:
        body_string = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    # STEP 4: Sign and attach the signature.
    query_params["sign"] = _make_signature(path, query_params, body_string=body_string)

    # STEP 5: Send the request.
    url = f"{config.TIKTOKSHOP_OPEN_API_BASE_URL}{path}"
    headers = {"x-tts-access-token": access_token}

    if method == "GET":
        return requests.get(url, params=query_params, headers=headers, timeout=30)

    if method == "POST":
        headers["content-type"] = "application/json"
        return requests.post(
            url,
            params=query_params,
            headers=headers,
            data=body_string.encode("utf-8"),
            timeout=60,
        )

    raise ValueError(f"Unsupported HTTP method: {method}")


def _safe_json(response):
    """Returns parsed JSON, or an empty dict if the response is not JSON."""
    try:
        return response.json()
    except ValueError:
        return {}


def _raise_on_api_error(response, data, context):
    """Raises RuntimeError if the response signals an API or HTTP error."""
    if response.status_code != 200:
        raise RuntimeError(
            f"{context} HTTP {response.status_code}: "
            f"{json.dumps(data, ensure_ascii=False) if data else response.text}"
        )
    code = data.get("code")
    if code not in (None, 0, "0"):
        raise RuntimeError(
            f"{context} API error code={code}: "
            f"{data.get('message') or data.get('msg') or data}"
        )


# ============================================================
# Shop cipher (must be fetched before any order/fulfillment call)
# ============================================================

def _get_shop_cipher():
    """
    Fetches the shop cipher for the configured shop and caches it for the
    remainder of this process run.

    TikTok Shop's newer Open API endpoints require shop_cipher in every call;
    the cipher is obtained by hitting /authorization/202309/shops with just
    the access token, shop_id, and the other basic params (NOT shop_cipher
    itself — there's your chicken-and-egg bootstrap).
    """

    global _cached_shop_cipher
    if _cached_shop_cipher:
        return _cached_shop_cipher

    # STEP 1: Call the authorized-shops endpoint WITHOUT shop_cipher.
    path = "/authorization/202309/shops"
    response = _call_signed("GET", path, include_cipher=False)
    data = _safe_json(response)
    _raise_on_api_error(response, data, context="authorized shops")

    # STEP 2: Find the shop that matches our configured TIKTOKSHOP_SHOP_ID.
    shops = data.get("data", {}).get("shops", []) or []
    matching_shop = next(
        (s for s in shops if str(s.get("id")) == str(config.TIKTOKSHOP_SHOP_ID)),
        None,
    )
    if matching_shop is None:
        raise RuntimeError(
            f"Shop {config.TIKTOKSHOP_SHOP_ID} not found in authorized shops. "
            f"Available shops: {[s.get('id') for s in shops]}"
        )

    cipher = matching_shop.get("cipher")
    if not cipher:
        raise RuntimeError(
            f"Authorized shops response had no cipher for shop "
            f"{config.TIKTOKSHOP_SHOP_ID}: {matching_shop}"
        )

    # STEP 3: Cache for the rest of this run.
    _cached_shop_cipher = cipher
    print(f"  Fetched shop cipher for shop {config.TIKTOKSHOP_SHOP_ID}")
    return cipher


# ============================================================
# Public function 1: get_pending_orders()
# ============================================================

def get_pending_orders():
    """
    Fetches all TikTok Shop orders in AWAITING_SHIPMENT or AWAITING_COLLECTION.

    The order search endpoint only accepts one status per call, so we make
    two searches and merge. We keep the original TikTok Shop order shape
    (not a normalized one) because downstream code in main.py needs to walk
    the nested `packages` array, and telegram_sender.build_caption reads
    item-level shipping_provider_name.

    Returns:
      A list of order dicts, de-duplicated by order id.
    """

    # STEP 0: Fake mode shortcut.
    if config.USE_FAKE_TIKTOKSHOP:
        from src import tiktokshop_client_fake
        return tiktokshop_client_fake.get_pending_orders()

    # STEP 1: Search each status separately and merge by order id.
    merged_orders_by_id = {}

    for status in _PENDING_STATUSES:
        print(f"  Searching TikTok Shop orders with status {status}...")
        status_orders = _search_orders_by_status(status)
        print(f"  Status {status}: {len(status_orders)} order(s) returned")

        for order in status_orders:
            order_id = _extract_order_id(order)
            if not order_id:
                continue
            # First status seen wins on duplicates. AWAITING_SHIPMENT is
            # checked first, so an order still needing shipping stays tagged
            # as such.
            if order_id not in merged_orders_by_id:
                merged_orders_by_id[order_id] = order

    return list(merged_orders_by_id.values())


def _search_orders_by_status(status):
    """Paginates through all orders for one specific status."""

    path = f"/order/{config.TIKTOKSHOP_API_VERSION}/orders/search"
    all_orders = []
    page_token = None

    while True:
        # STEP 1: Build request.
        body = {"order_status": status}
        extra_query = {"page_size": "50"}
        if page_token:
            extra_query["page_token"] = page_token

        # STEP 2: Call the endpoint.
        response = _call_signed("POST", path, extra_query=extra_query, body=body)
        data = _safe_json(response)
        _raise_on_api_error(response, data, context=f"order search ({status})")

        # STEP 3: Collect the page.
        payload = data.get("data", {}) or {}
        page_orders = (
                payload.get("orders")
                or payload.get("order_list")
                or payload.get("list")
                or []
        )
        if isinstance(page_orders, list):
            all_orders.extend(page_orders)

        # STEP 4: Continue only while TikTok Shop hands us a next_page_token
        # AND signals more.
        next_token = (
                payload.get("next_page_token")
                or payload.get("page_token")
                or payload.get("nextPageToken")
        )
        has_more = bool(payload.get("more") or payload.get("has_more") or next_token)

        if not has_more or not next_token:
            break

        page_token = next_token
        time.sleep(0.3)  # Be polite between pages.

    return all_orders


def _extract_order_id(order):
    """Returns the order id from whatever field TikTok Shop used."""
    value = order.get("id") or order.get("order_id") or order.get("orderId")
    return str(value) if value else None


# ============================================================
# Public function 2: ship_packages()
# ============================================================

def ship_packages(package_ids):
    """
    Tells TikTok Shop to mark the given packages as ready to ship
    (Batch Ship Packages). Equivalent to the old Shopee "arrange shipment"
    step, but at package granularity instead of order granularity.

    Args:
      package_ids: a list of package id strings. All shipped in one request.

    Raises:
      RuntimeError if TikTok Shop rejects the whole batch. Partial failures
      (individual package-level errors in the response body) will surface
      in the response payload; main.py treats them best-effort and lets the
      next scheduled run retry stragglers.
    """

    # STEP 0: Fake mode shortcut.
    if config.USE_FAKE_TIKTOKSHOP:
        from src import tiktokshop_client_fake
        return tiktokshop_client_fake.ship_packages(package_ids)

    # STEP 1: Nothing to do if the list is empty.
    if not package_ids:
        return

    # STEP 2: Build the body exactly as the verified curl example.
    path = f"/fulfillment/{config.TIKTOKSHOP_API_VERSION}/packages/ship"
    body = {
        "packages": [{"id": str(package_id)} for package_id in package_ids],
    }

    # STEP 3: Send the batch request.
    response = _call_signed("POST", path, body=body)
    data = _safe_json(response)
    _raise_on_api_error(response, data, context="batch ship packages")


# ============================================================
# Public function 3: get_waybill_pdf()
# ============================================================

def get_waybill_pdf(package_id):
    """
    Generates and downloads the waybill PDF for a single package.

    Two-step process (matching scripts/process_waybill.py):
      1. GET /fulfillment/{version}/packages/{id}/shipping_documents
         -> returns a doc_url pointing to the rendered PDF.
      2. Download the PDF from doc_url (this second call does NOT need
         TikTok Shop signing - the doc_url is already pre-signed).

    We retry a small number of times in case TikTok Shop is still rendering
    the PDF when we ask. If it's still not ready, we return None and let
    the next scheduled run try again.

    Returns:
      PDF bytes if ready, or None if not ready yet.
    """

    # STEP 0: Fake mode shortcut.
    if config.USE_FAKE_TIKTOKSHOP:
        from src import tiktokshop_client_fake
        return tiktokshop_client_fake.get_waybill_pdf(package_id)

    # STEP 1: Try a few times with short delays.
    path = f"/fulfillment/{config.TIKTOKSHOP_API_VERSION}/packages/{package_id}/shipping_documents"
    extra_query = {"document_type": config.TIKTOKSHOP_DOCUMENT_TYPE}

    for attempt in range(3):
        if attempt > 0:
            time.sleep(5)

        try:
            # STEP 1a: Ask TikTok Shop for the doc_url.
            response = _call_signed("GET", path, extra_query=extra_query)
            data = _safe_json(response)

            if response.status_code != 200 or data.get("code") not in (0, "0", None):
                print(
                    f"  Waybill for {package_id} not ready "
                    f"(attempt {attempt + 1}/3): {data.get('message') or data}"
                )
                continue

            doc_url = (data.get("data") or {}).get("doc_url", "")
            doc_url = doc_url.strip() if isinstance(doc_url, str) else ""
            if not doc_url:
                print(
                    f"  Waybill for {package_id} returned no doc_url "
                    f"(attempt {attempt + 1}/3)"
                )
                continue

            # STEP 1b: Download the PDF from the pre-signed URL.
            pdf_bytes = _download_pdf_from_doc_url(doc_url)
            if pdf_bytes is None:
                print(
                    f"  Waybill PDF for {package_id} did not download cleanly "
                    f"(attempt {attempt + 1}/3)"
                )
                continue

            return pdf_bytes

        except Exception as e:
            # Any unexpected exception is treated like "not ready yet" - we
            # retry within this run, then let the next scheduled run handle it.
            print(f"  Waybill error for {package_id} (attempt {attempt + 1}/3): {e}")

    # STEP 2: Give up for this run.
    print(f"  Waybill for {package_id} still not ready, will retry next run")
    return None


def _download_pdf_from_doc_url(doc_url):
    """
    Downloads the PDF from a doc_url returned by TikTok Shop.

    Returns:
      bytes if the download produced a valid PDF, else None.
    """

    response = requests.get(doc_url, timeout=60)
    if response.status_code != 200:
        return None

    # Most of the time TikTok Shop sends application/pdf. Sometimes content-type
    # is missing or generic, so we also check the file signature as a fallback.
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/pdf" in content_type:
        return response.content
    if response.content.startswith(b"%PDF"):
        return response.content

    return None