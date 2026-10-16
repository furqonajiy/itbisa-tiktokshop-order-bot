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

It never sends to Telegram and never touches processed_orders.json /
order_items.json, so the next /resi_tiktok still sends these waybills. Pass
package ids, or the order id of a pending order (expanded to its packages).
A waybill exists only once the package is shipped and only until the courier
picks it up. With --atur-kirim, packages of a pending AWAITING_SHIPMENT order
are shipped first, exactly as /resi_tiktok would, which is a marketplace
write: use it only with the operator's go-ahead for those orders.

Usage:
  python scripts/fetch_label_sample.py [--atur-kirim] ID [ID ...]
"""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import tiktokshop_client  # noqa: E402

OUT_DIR = PROJECT_ROOT / "out"


def resolve(ids, atur_kirim):
    """Expands pending order ids to package ids; ships them when asked."""
    pending = {o["id"]: o for o in tiktokshop_client.get_pending_orders()}
    package_ids, to_ship = [], []
    for i in ids:
        order = pending.get(i)
        if order is None:
            package_ids.append(i)
            continue
        pids = [p["id"] for p in order.get("packages") or []]
        package_ids += pids
        if atur_kirim and order.get("status") == "AWAITING_SHIPMENT":
            to_ship += pids
    if to_ship:
        tiktokshop_client.ship_packages(to_ship)
        print(f"  pengiriman diatur: {' '.join(to_ship)}")
        time.sleep(5)  # let TikTok Shop start generating the waybill
    return package_ids


def main(ids, atur_kirim=False):
    OUT_DIR.mkdir(exist_ok=True)
    package_ids = resolve(ids, atur_kirim)
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
    args = sys.argv[1:]
    atur = "--atur-kirim" in args
    args = [a for a in args if a != "--atur-kirim"]
    if not args:
        sys.exit(__doc__)
    sys.exit(main(args, atur_kirim=atur))
