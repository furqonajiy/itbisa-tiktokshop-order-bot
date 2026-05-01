"""
telegram_sender.py
------------------
Sends shipping label images and status messages to the Telegram chat.

Public functions (used by main.py):
  - send_label(png_pages, caption) -> True/False
  - send_summary(text) -> True/False
  - build_caption(order) -> string
  - build_summary(time_hhmm, success_count, skipped_count) -> string
  - build_safety_stop_message(time_hhmm, order_count, max_allowed) -> string

Caption shape:
  📦 {order_id}
  🚚 {courier}

  Barang:
    • {qty} x {sku}

Each entry in order.line_items represents ONE unit, so qty per SKU is just
the number of matching rows. Courier comes from each row's
shipping_provider_name. If an order has multiple distinct couriers, the
courier is appended inline to each SKU line so the warehouse can tell which
shipment a SKU belongs to.
"""

from collections import OrderedDict

import requests

from src import config

_TELEGRAM_API_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


# ============================================================
# Sending
# ============================================================

def send_label(png_pages, caption):
    """Sends one or more label images. Returns True only if all delivered.

    main.py uses the return value to decide whether to mark a package as
    processed, so we return bool rather than raising. Multi-page PDFs are
    already merged by label_processor into two PDF pages per Telegram image.
    """
    if isinstance(png_pages, (bytes, bytearray)):
        png_pages = [bytes(png_pages)]
    else:
        png_pages = list(png_pages)

    if not png_pages:
        print("  Telegram send skipped: no label pages to send")
        return False

    url = f"{_TELEGRAM_API_URL}/sendPhoto"
    total = len(png_pages)

    for index, png_bytes in enumerate(png_pages, start=1):
        # Full caption on image 1; short part-number label on the rest.
        if total > 1:
            if index == 1:
                page_caption = f"{caption}\n\n🧾 Bagian {index}/{total}"
            else:
                page_caption = f"🧾 Bagian {index}/{total}"
        else:
            page_caption = caption

        files = {"photo": (f"label_{index}.png", png_bytes, "image/png")}
        data = {"chat_id": config.TELEGRAM_CHAT_ID, "caption": page_caption}

        try:
            response = requests.post(url, files=files, data=data, timeout=30)
        except requests.RequestException as e:
            print(f"  Telegram request failed on image {index}: {e}")
            return False

        if response.status_code != 200 or not response.json().get("ok"):
            print(f"  Telegram rejected image {index}: HTTP {response.status_code} {response.text}")
            return False

    return True


def send_summary(text):
    """Sends a plain-text message. No retry; next scheduled run will send another."""
    url = f"{_TELEGRAM_API_URL}/sendMessage"
    data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}

    try:
        response = requests.post(url, data=data, timeout=30)
    except requests.RequestException as e:
        print(f"  Telegram summary failed: {e}")
        return False

    if response.status_code != 200:
        print(f"  Telegram returned {response.status_code}: {response.text}")
        return False

    return True


# ============================================================
# Caption building
# ============================================================

def build_caption(order):
    """Builds the per-label caption in Bahasa Indonesia."""
    order_id = order["id"]

    # Group by SKU, counting rows (each row = 1 unit) and collecting couriers.
    # OrderedDict keeps first-seen order for stable caption output.
    groups = OrderedDict()  # sku -> {"qty": int, "couriers": [str]}
    distinct_couriers = []

    for item in order["line_items"]:
        sku = item["seller_sku"]
        courier = item["shipping_provider_name"].strip()

        if sku not in groups:
            groups[sku] = {"qty": 0, "couriers": []}
        groups[sku]["qty"] += 1

        if courier and courier not in groups[sku]["couriers"]:
            groups[sku]["couriers"].append(courier)
        if courier and courier not in distinct_couriers:
            distinct_couriers.append(courier)

    courier_line = " / ".join(distinct_couriers) if distinct_couriers else "?"

    # If an order has multiple distinct couriers, inline each SKU's courier
    # so the warehouse can tell which shipment a SKU belongs to.
    multi_courier = len(distinct_couriers) > 1
    item_lines = []
    for sku, info in groups.items():
        if multi_courier and info["couriers"]:
            courier_text = " / ".join(info["couriers"])
            item_lines.append(f"  • {info['qty']} x {sku} ({courier_text})")
        else:
            item_lines.append(f"  • {info['qty']} x {sku}")

    items_text = "\n".join(item_lines) if item_lines else "  (tidak ada barang)"

    return (
        f"📦 {order_id}\n"
        f"🚚 {courier_line}\n"
        f"\n"
        f"Barang:\n"
        f"{items_text}"
    )


def build_summary(time_hhmm, success_count, skipped_count):
    """Heartbeat message in Bahasa Indonesia."""
    if success_count == 0 and skipped_count == 0:
        return f"✅ TikTok Shop - {time_hhmm} - Tidak ada pesanan baru"
    if skipped_count == 0:
        return f"✅ TikTok Shop - {time_hhmm} - {success_count} label terkirim"
    return (
        f"⚠️ TikTok Shop - {time_hhmm} - {success_count} terkirim, "
        f"{skipped_count} gagal (akan dicoba lagi)"
    )


def build_safety_stop_message(time_hhmm, package_count, max_allowed):
    """Alert when too many new packages appear at once — needs human review."""
    return (
        f"⚠️ {time_hhmm} - PERINGATAN: {package_count} paket/resi baru "
        f"melebihi batas {max_allowed}. Mohon dicek dahulu sebelum diproses."
    )
