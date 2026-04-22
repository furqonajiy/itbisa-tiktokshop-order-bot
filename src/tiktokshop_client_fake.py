"""
tiktokshop_client_fake.py
-------------------------
Fake replacement for tiktokshop_client.py. Used only during local development
when the real TikTok Shop API is not accessible.

Why this file exists:
  We want to test the full pipeline (fetch orders -> get label -> convert to
  PNG -> send to Telegram) without depending on the real TikTok Shop API.
  This file provides the same public functions as tiktokshop_client.py,
  but returns canned data and generates dummy PDF labels in memory.

How to use it:
  Set USE_FAKE_TIKTOKSHOP=true in your .env file. The dispatcher in
  tiktokshop_client.py will route calls here automatically.
"""

import io
from datetime import datetime, timezone

from reportlab.lib.pagesizes import A6
from reportlab.pdfgen import canvas


# ============================================================
# Canned fake orders.
# ============================================================

_FAKE_ORDERS = [
    {
        "order_id": "576461413038785750",
        "order_status": "AWAITING_COLLECTION",  # Already marked RTS, label is ready
        "shipping_carrier": "TikTok Shipping",
        "item_list": [
            {
                "item_name": "ITBisa - Socket IC DIP 16 Pin 2.54mm Narrow 2x8",
                "item_sku": "ITBISA-SOCKET-IC-DIP16-NARROW",
                "model_name": "",
                "model_sku": "ITBISA-SOCKET-IC-DIP16-NARROW",
                "model_quantity_purchased": 10,
            }
        ],
    },
    {
        "order_id": "576461413038785751",
        "order_status": "AWAITING_SHIPMENT",  # Needs ship_order first
        "shipping_carrier": "TikTok Shipping",
        "item_list": [
            {
                "item_name": "ITBisa - LED Super Bright 5mm All Color",
                "item_sku": "ITBISA-LED-SUPERBRIGHT-5MM-RED",
                "model_name": "Red",
                "model_sku": "ITBISA-LED-SUPERBRIGHT-5MM-RED",
                "model_quantity_purchased": 20,
            },
            {
                "item_name": "ITBisa - LED Super Bright 5mm All Color",
                "item_sku": "ITBISA-LED-SUPERBRIGHT-5MM-GREEN",
                "model_name": "Green",
                "model_sku": "ITBISA-LED-SUPERBRIGHT-5MM-GREEN",
                "model_quantity_purchased": 15,
            },
        ],
    },
    {
        "order_id": "576461413038785752",
        "order_status": "AWAITING_SHIPMENT",  # Needs ship_order first
        "shipping_carrier": "TikTok Shipping",
        "item_list": [
            {
                "item_name": "ITBisa - Resistor Pack 1/4W 1% Toleransi 100 Nilai",
                "item_sku": "ITBISA-RES-PACK-100",
                "model_name": "",
                "model_sku": "ITBISA-RES-PACK-100",
                "model_quantity_purchased": 1,
            }
        ],
    },
]


# Tracks which fake orders have been "shipped" during this run.
_shipped_in_this_run = set()


# ============================================================
# Public functions (same signatures as tiktokshop_client.py)
# ============================================================

def get_pending_orders():
    """Returns the canned list of fake orders."""

    # STEP 1: Print a clear marker so we never confuse fake runs with real ones.
    print(f"  [FAKE MODE] Returning {len(_FAKE_ORDERS)} canned fake TikTok Shop orders")

    # STEP 2: Return a shallow copy so callers do not mutate our source list.
    return [dict(order) for order in _FAKE_ORDERS]



def ship_order(order_id):
    """
    Simulates calling TikTok Shop's ready-to-ship endpoint.

    In real TikTok Shop, this would move the order from AWAITING_SHIPMENT to
    AWAITING_COLLECTION and trigger label availability. In our fake, we just
    record that this order has been marked ready to ship.
    """

    # STEP 1: Verify the order exists in our fake data.
    order = next((o for o in _FAKE_ORDERS if o["order_id"] == order_id), None)
    if order is None:
        raise ValueError(f"[FAKE MODE] Unknown order_id: {order_id}")

    # STEP 2: Mark it as shipped in our in-memory state.
    _shipped_in_this_run.add(order_id)
    print(f"  [FAKE MODE] Marked {order_id} as ready to ship")



def get_shipping_label_pdf(order_id):
    """
    Generates a dummy PDF that looks roughly like a shipping label.

    For AWAITING_SHIPMENT orders, the label is only available AFTER
    ship_order has been called. For AWAITING_COLLECTION orders, the label
    is available immediately.
    """

    # STEP 1: Look up the order in our canned data.
    order = next((o for o in _FAKE_ORDERS if o["order_id"] == order_id), None)
    if order is None:
        print(f"  [FAKE MODE] Unknown order_id: {order_id}")
        return None

    # STEP 2: Check if the label is actually available yet.
    if order["order_status"] == "AWAITING_SHIPMENT" and order_id not in _shipped_in_this_run:
        print(f"  [FAKE MODE] {order_id} not ready-to-ship yet, label unavailable")
        return None

    print(f"  [FAKE MODE] Generating dummy PDF for {order_id}")

    # STEP 3: Create an in-memory PDF using reportlab.
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A6)
    width, height = A6

    # STEP 4: Draw a header bar at the top.
    pdf.setFillColorRGB(0.00, 0.00, 0.00)
    pdf.rect(0, height - 40, width, 40, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(15, height - 27, "ITBisa - TikTok Label (FAKE)")

    # STEP 5: Draw order details.
    pdf.setFillColorRGB(0, 0, 0)
    y = height - 65
    line_height = 14

    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(15, y, "Order ID:")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(15, y - line_height, order["order_id"])
    y -= line_height * 2 + 8

    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(15, y, "Kurir:")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(15, y - line_height, order["shipping_carrier"])
    y -= line_height * 2 + 8

    # STEP 6: Draw the items.
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(15, y, "Barang:")
    y -= line_height
    pdf.setFont("Helvetica", 8)
    for item in order["item_list"]:
        line = f"{item['model_quantity_purchased']} x {item['model_sku']}"
        pdf.drawString(15, y, line)
        y -= line_height - 2

    y -= 8

    # STEP 7: Draw a fake barcode area.
    pdf.setFillColorRGB(0, 0, 0)
    pdf.rect(15, y - 50, width - 30, 40, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica", 8)
    pdf.drawCentredString(width / 2, y - 33, f"|||| {order['order_id']} ||||")

    # STEP 8: Footer with timestamp so we can verify the PDF was freshly generated.
    pdf.setFillColorRGB(0.5, 0.5, 0.5)
    pdf.setFont("Helvetica-Oblique", 7)
    pdf.drawString(
        15,
        15,
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
    )

    # STEP 9: Finalize the PDF and return its bytes.
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
