"""
telegram_sender.py
------------------
Sends shipping label (waybill) images to the Telegram bot.

Public functions (used by main.py):
  - send_label(png_bytes_or_pages, caption) -> True/False
  - send_summary(text) -> True/False
  - build_caption(order) -> string
  - build_summary(time_hhmm, success_count, skipped_count) -> string
  - build_safety_stop_message(time_hhmm, order_count, max_allowed) -> string

Caption rules (follow process_waybill.py):
  - order id comes from id / order_id / orderId
  - courier comes ONLY from item-level shipping_provider_name fields
  - items are grouped by SKU; repeated SKUs are summed
  - if multiple item-level couriers exist, courier is appended to each line
"""

import requests

from src import config


# The Telegram Bot API base URL. We compose full endpoints from this.
_TELEGRAM_API_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


# ============================================================
# Public sending functions
# ============================================================

def send_label(png_bytes_or_pages, caption):
    """
    Sends one or more label images to the Telegram chat.

    Accepts either a single bytes object or a list of bytes objects.
    Returns True only if every page delivered successfully.

    Why a bool return (vs raising):
      main.py uses this return value to decide whether to mark a package as
      processed. A bool keeps the call site simple and avoids wrapping every
      call in try/except.
    """

    # STEP 1: Normalize to a list of pages.
    if isinstance(png_bytes_or_pages, (bytes, bytearray)):
        png_pages = [bytes(png_bytes_or_pages)]
    else:
        png_pages = list(png_bytes_or_pages)

    total_pages = len(png_pages)
    if total_pages == 0:
        print("  Telegram send skipped: no label pages to send")
        return False

    # STEP 2: Send each page in order using Telegram's sendPhoto endpoint.
    url = f"{_TELEGRAM_API_URL}/sendPhoto"
    for page_index, png_bytes in enumerate(png_pages, start=1):
        files = {
            "photo": (f"label_{page_index}.png", png_bytes, "image/png"),
        }

        # The full caption only appears on the first page. Subsequent pages
        # get a short "Halaman N/M" label so the employee knows they belong
        # together.
        page_caption = caption
        if total_pages > 1:
            page_label = f"🧾 Halaman {page_index}/{total_pages}"
            page_caption = f"{caption}\n\n{page_label}" if page_index == 1 else page_label

        data = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "caption": page_caption,
        }

        # STEP 3: Send. Catch transport errors so we can return False cleanly.
        try:
            response = requests.post(url, files=files, data=data, timeout=30)
        except requests.RequestException as e:
            print(f"  Telegram request failed on page {page_index}: {e}")
            return False

        # STEP 4: Check the response.
        if response.status_code != 200:
            print(
                f"  Telegram returned status {response.status_code} "
                f"on page {page_index}: {response.text}"
            )
            return False

        response_json = response.json()
        if not response_json.get("ok"):
            print(f"  Telegram rejected page {page_index}: {response_json}")
            return False

    return True


def send_summary(text):
    """
    Sends a plain-text status/heartbeat message. We do not retry on failure
    because the next scheduled run will send another summary anyway.
    """

    url = f"{_TELEGRAM_API_URL}/sendMessage"
    data = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
    }

    try:
        response = requests.post(url, data=data, timeout=30)
    except requests.RequestException as e:
        print(f"  Telegram summary failed: {e}")
        return False

    if response.status_code != 200:
        print(f"  Telegram returned status {response.status_code}: {response.text}")
        return False

    return True


# ============================================================
# Caption builder (follows process_waybill.py logic)
# ============================================================

def build_caption(order):
    """
    Builds the per-label caption in Bahasa Indonesia.

    Output shape:
      📦 {order_id}
      🚚 {courier}

      Barang:
        • {qty} x {sku}

    Grouping: identical SKUs across item rows are summed into one line.
    Multiple couriers: if different item rows report different
      shipping_provider_name values, we append each SKU's courier(s) inline.
    """

    # STEP 1: Pull the order id.
    order_id = (
            order.get("id")
            or order.get("order_id")
            or order.get("orderId")
            or "?"
    )

    # STEP 2: Build grouped item lines + collect distinct item-level couriers.
    item_lines, distinct_couriers = _extract_grouped_item_lines(order)

    # STEP 3: Top courier line comes ONLY from item-level shipping_provider_name.
    courier = " / ".join(distinct_couriers) if distinct_couriers else "?"
    items_text = "\n".join(item_lines) if item_lines else "  (tidak ada barang)"

    # STEP 4: Assemble.
    return (
        f"📦 {order_id}\n"
        f"🚚 {courier}\n"
        f"\n"
        f"Barang:\n"
        f"{items_text}"
    )


def build_summary(time_hhmm, success_count, skipped_count):
    """
    Heartbeat message in Bahasa Indonesia.

    Patterns:
      - 0/0:     "✅ 11:00 - Tidak ada pesanan baru"
      - all OK:  "✅ 12:00 - 3 label terkirim"
      - partial: "⚠️ 13:00 - 2 terkirim, 1 gagal (akan dicoba lagi)"
    """

    if success_count == 0 and skipped_count == 0:
        return f"✅ {time_hhmm} - Tidak ada pesanan baru"

    if skipped_count == 0:
        return f"✅ {time_hhmm} - {success_count} label terkirim"

    return (
        f"⚠️ {time_hhmm} - {success_count} terkirim, "
        f"{skipped_count} gagal (akan dicoba lagi)"
    )


def build_safety_stop_message(time_hhmm, order_count, max_allowed):
    """Alert when we see suspiciously many new packages - needs human eyes."""
    return (
        f"⚠️ {time_hhmm} - PERINGATAN: {order_count} pesanan baru "
        f"melebihi batas {max_allowed}. Mohon dicek dahulu sebelum diproses."
    )


# ============================================================
# Internal helpers (grouping + field extraction)
# ============================================================

def _extract_grouped_item_lines(order):
    """
    Returns:
      (list of caption lines, list of distinct couriers in first-seen order)

    Groups items by SKU, sums quantities, and collects distinct couriers
    from item-level shipping_provider_name only.
    """

    # STEP 1: Pull item rows from known container keys.
    rows = _extract_item_rows_from_known_containers(order)
    if not rows:
        return [], []

    # STEP 2: Aggregate by SKU.
    grouped = {}
    distinct_couriers = []
    distinct_courier_set = set()

    for row in rows:
        sku = row["sku"]
        qty = row["qty"]
        courier = row["courier"]

        if sku not in grouped:
            grouped[sku] = {
                "qty": 0,
                "couriers": [],
                "courier_set": set(),
            }

        grouped[sku]["qty"] += qty

        if courier and courier not in grouped[sku]["courier_set"]:
            grouped[sku]["courier_set"].add(courier)
            grouped[sku]["couriers"].append(courier)

        if courier and courier not in distinct_courier_set:
            distinct_courier_set.add(courier)
            distinct_couriers.append(courier)

    # STEP 3: Build one line per SKU. If multiple distinct couriers exist
    # across all rows, inline each SKU's courier(s) so the employee can tell
    # which shipment a SKU belongs to.
    multiple_distinct_couriers = len(distinct_couriers) > 1
    lines = []
    for sku, data in grouped.items():
        total_qty = data["qty"]
        sku_couriers = data["couriers"]
        if multiple_distinct_couriers and sku_couriers:
            courier_text = " / ".join(sku_couriers)
            lines.append(f"  • {total_qty} x {sku} ({courier_text})")
        else:
            lines.append(f"  • {total_qty} x {sku}")

    return lines, distinct_couriers


def _extract_item_rows_from_known_containers(order):
    """
    Walks the order and pulls item rows only from known item containers:
      - line_items / lineItems
      - item_list
      - sku_list

    This matters because we only want courier from item-level
    shipping_provider_name, not from random order-level fields like
    delivery_option_name.
    """

    # STEP 1: Collect every known item container list.
    item_lists = []
    _collect_known_item_lists_recursive(order, item_lists)

    # STEP 2: Normalize each item dict into a row.
    rows = []
    for item_list in item_lists:
        for item in item_list:
            if not isinstance(item, dict):
                continue
            rows.append({
                "sku": _pick_sku(item),
                "qty": _normalize_quantity(
                    item.get("quantity")
                    or item.get("model_quantity_purchased")
                    or item.get("count")
                    or 1
                ),
                "courier": _pick_item_level_shipping_provider_name(item),
            })

    return rows


def _collect_known_item_lists_recursive(value, output_lists):
    """Recursively finds known-item-container lists and appends them."""

    if isinstance(value, dict):
        for key, nested in value.items():
            if key in ("line_items", "lineItems", "item_list", "sku_list"):
                if isinstance(nested, list):
                    output_lists.append(nested)
            _collect_known_item_lists_recursive(nested, output_lists)
    elif isinstance(value, list):
        for item in value:
            _collect_known_item_lists_recursive(item, output_lists)


def _pick_item_level_shipping_provider_name(item):
    """Returns courier ONLY from item-level shipping_provider_name fields."""

    if not isinstance(item, dict):
        return None
    courier = (
            item.get("shipping_provider_name")
            or item.get("shippingProviderName")
    )
    if courier is None:
        return None
    courier = str(courier).strip()
    return courier or None


def _pick_sku(item):
    """
    Picks the most specific SKU for an item.

    Preference order:
      1. model_sku    (variant SKU)
      2. item_sku     (parent product SKU)
      3. seller_sku   (TikTok Shop's common field)
      4. sku_id       (last-resort numeric id)
      5. product_name / item_name
      6. "(tidak ada nama)"

    Warehouse shelves are organized by SKU, so we prefer SKU over name.
    """

    return (
            (item.get("model_sku") or "").strip()
            or (item.get("item_sku") or "").strip()
            or (item.get("seller_sku") or "").strip()
            or str(item.get("sku_id") or "").strip()
            or item.get("product_name")
            or item.get("item_name")
            or "(tidak ada nama)"
    )


def _normalize_quantity(value):
    """Coerces a quantity value into an int safely (handles '2', '2.0', etc.)."""

    if isinstance(value, int):
        return value
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 1