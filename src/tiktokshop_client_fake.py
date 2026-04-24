"""
tiktokshop_client_fake.py
-------------------------
Fake replacement for tiktokshop_client.py. Used only during local development
when real TikTok Shop API access is not available.

The orders here follow the real TikTok Shop response shape:
  - top-level order.id
  - order.order_status ("AWAITING_SHIPMENT" or "AWAITING_COLLECTION")
  - order.packages = [{"id": "..."}]
  - order.line_items = [{seller_sku, product_name, quantity, shipping_provider_name}]

That lets main.py, tiktokshop_client.ship_packages, and
telegram_sender.build_caption exercise the exact same code paths as
production, just with no network calls.
"""

import io
from datetime import datetime, timezone

from reportlab.lib.pagesizes import A6
from reportlab.pdfgen import canvas


# ============================================================
# Canned fake orders
# ============================================================

_FAKE_ORDERS = [
    {
        "id": "576461413038785750",
        "order_status": "AWAITING_COLLECTION",
        "packages": [{"id": "1193892791888217574"}],
        "line_items": [
            {
                "seller_sku": "ITBISA-SOCKET-IC-DIP16-NARROW",
                "product_name": "ITBisa - Socket IC DIP 16 Pin 2.54mm Narrow",
                "quantity": 10,
                "shipping_provider_name": "JNT Express",
            }
        ],
    },
    {
        "id": "576461413038785751",
        "order_status": "AWAITING_SHIPMENT",
        "packages": [{"id": "1193892791888217575"}],
        "line_items": [
            {
                "seller_sku": "ITBISA-LED-SUPERBRIGHT-5MM-RED",
                "product_name": "ITBisa - LED Super Bright 5mm Red",
                "quantity": 20,
                "shipping_provider_name": "SPX Express",
            },
            {
                "seller_sku": "ITBISA-LED-SUPERBRIGHT-5MM-GREEN",
                "product_name": "ITBisa - LED Super Bright 5mm Green",
                "quantity": 15,
                "shipping_provider_name": "SPX Express",
            },
        ],
    },
    {
        "id": "576461413038785752",
        "order_status": "AWAITING_SHIPMENT",
        "packages": [{"id": "1193892791888217576"}],
        "line_items": [
            {
                "seller_sku": "ITBISA-RES-PACK-100",
                "product_name": "ITBisa - Resistor Pack 100 values",
                "quantity": 1,
                "shipping_provider_name": "J&T Express",
            }
        ],
    },
]


# Tracks which fake packages have been "shipped" during this run so the
# fake waybill endpoint can behave realistically.
_shipped_in_this_run = set()


# ============================================================
# Public functions (same signatures as tiktokshop_client.py)
# ============================================================

def get_pending_orders():
    """Returns the canned list of fake orders (shallow-copied)."""
    print(f"  [FAKE MODE] Returning {len(_FAKE_ORDERS)} canned fake TikTok Shop orders")
    return [dict(order) for order in _FAKE_ORDERS]


def ship_packages(package_ids):
    """Simulates Batch Ship Packages by tagging ids as 'shipped this run'."""
    for package_id in package_ids:
        _shipped_in_this_run.add(str(package_id))
    print(f"  [FAKE MODE] Batch-shipped {len(package_ids)} package(s)")


def get_waybill_pdf(package_id):
    """
    Returns a dummy PDF for an already-shipped package, else None.

    Mirrors the real behavior: packages from AWAITING_SHIPMENT orders only
    have waybills AFTER ship_packages has been called for them; packages
    from AWAITING_COLLECTION orders always have waybills available.
    """

    # STEP 1: Find the source order and check package existence.
    source_order = _find_source_order(package_id)
    if source_order is None:
        print(f"  [FAKE MODE] Unknown package_id: {package_id}")
        return None

    # STEP 2: Gate waybill availability on whether the package was shipped
    # this run (if the source order was AWAITING_SHIPMENT).
    status = source_order.get("order_status")
    if status == "AWAITING_SHIPMENT" and str(package_id) not in _shipped_in_this_run:
        print(f"  [FAKE MODE] Package {package_id} not shipped yet, waybill unavailable")
        return None

    # STEP 3: Generate a small realistic-looking PDF.
    print(f"  [FAKE MODE] Generating dummy waybill PDF for package {package_id}")
    return _generate_dummy_pdf(source_order, str(package_id))


# ============================================================
# Internal helpers
# ============================================================

def _find_source_order(package_id):
    """Finds the order whose packages contain the given package id."""
    for order in _FAKE_ORDERS:
        for package in order.get("packages", []):
            if str(package.get("id")) == str(package_id):
                return order
    return None


def _generate_dummy_pdf(order, package_id):
    """Renders a tiny A6 PDF in memory so the Telegram side has real bytes."""

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A6)
    width, height = A6

    # STEP 1: Header bar.
    pdf.setFillColorRGB(0.00, 0.00, 0.00)
    pdf.rect(0, height - 40, width, 40, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(15, height - 27, "ITBisa - TikTok Waybill (FAKE)")

    # STEP 2: Order and package info.
    pdf.setFillColorRGB(0, 0, 0)
    y = height - 65
    line_height = 14

    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(15, y, "Order ID:")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(15, y - line_height, str(order.get("id", "?")))
    y -= line_height * 2 + 4

    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(15, y, "Package ID:")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(15, y - line_height, package_id)
    y -= line_height * 2 + 8

    # STEP 3: Items.
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(15, y, "Barang:")
    y -= line_height
    pdf.setFont("Helvetica", 8)
    for item in order.get("line_items", []):
        line = f"{item.get('quantity', 1)} x {item.get('seller_sku', '?')}"
        pdf.drawString(15, y, line)
        y -= line_height - 2
    y -= 8

    # STEP 4: Fake barcode strip.
    pdf.setFillColorRGB(0, 0, 0)
    pdf.rect(15, y - 50, width - 30, 40, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica", 8)
    pdf.drawCentredString(width / 2, y - 33, f"|||| {package_id} ||||")

    # STEP 5: Footer timestamp so we can eyeball PDF freshness.
    pdf.setFillColorRGB(0.5, 0.5, 0.5)
    pdf.setFont("Helvetica-Oblique", 7)
    pdf.drawString(
        15,
        15,
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
    )

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()