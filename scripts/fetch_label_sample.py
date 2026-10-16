"""
fetch_label_sample.py
---------------------
Downloads the waybill PDF of already-labelled packages into out/, untouched.

Why this file exists:
  Telegram only ever receives the rendered PNG, so the original PDF (its text
  layer and exact column positions) is otherwise never kept. Changing how a
  label is rendered needs real PDFs to test against. The workflow
  label_sample.yml runs this, encrypts each PDF and uploads it as a one-day
  artifact: the repo is public and a label carries the buyer's name, address
  and phone.

It never ships a package, never sends to Telegram and never touches
processed_orders.json / order_items.json (the shipping-document call is a
GET). Pass package ids (order_items.json `package_ids`), not order ids.

Usage:
  python scripts/fetch_label_sample.py PACKAGE_ID [PACKAGE_ID ...]
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import tiktokshop_client  # noqa: E402

OUT_DIR = PROJECT_ROOT / "out"


def main(package_ids):
    OUT_DIR.mkdir(exist_ok=True)
    failed = 0
    for package_id in package_ids:
        try:
            pdf_bytes = tiktokshop_client.get_waybill_pdf(package_id)
        except Exception as e:                              # noqa: BLE001
            print(f"✗ {package_id}: {e}")
            failed += 1
            continue
        if pdf_bytes is None:
            print(f"✗ {package_id}: resi belum siap")
            failed += 1
            continue
        (OUT_DIR / f"{package_id}.pdf").write_bytes(pdf_bytes)
        print(f"✓ {package_id}: {len(pdf_bytes)} bytes")
    return 1 if failed == len(package_ids) else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
