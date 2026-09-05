"""
order_items.py
--------------
Record WHAT was ordered and WHICH parcel it left in, into
`data/order_items.json` on the `bot-state` branch.

Mirror of the Shopee order bot's file, and it exists for the same reason: a stock
opname taken while orders are awaiting shipment counts a shelf whose goods are
already picked and packed, while the book still counts them as on hand because
the order has not reached the Jual export yet. The difference looks like theft.
On 2026-09-05 that was 583 pcs across three SKUs, and telling a write-off from a
shipment in progress meant opening six orders by hand.

**Two things differ from Shopee and both matter:**

1. **Quantity is the ROW COUNT, not a field.** TikTok Shop returns one
   `line_items` entry per unit — `telegram_sender.build_caption` already counts
   rows (`groups[sku]["qty"] += 1`). Reading a `quantity` key here would silently
   record 1 for an order of 50.
2. **The parcel handle is `package_id`, not a resi.** TikTok works in packages and
   the bot fetches waybills by package id; one order can span several. The
   carrier's own tracking number is recorded too when the payload carries it, but
   `package_id` is the identifier that is always present and is what the bot
   itself uses.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "order_items.json"


def extract(order: dict) -> list[dict]:
    """[{sku, qty}] for one order, counting ROWS per seller_sku.

    TikTok Shop returns one `line_items` entry per unit, so qty is the number of
    rows carrying that SKU. `seller_sku` is normalized `UPPER().strip()` to the
    repo-wide SKU contract, or the report side cannot match its own ledger.
    """
    totals: dict[str, int] = {}
    for item in order.get("line_items") or []:
        sku = str(item.get("seller_sku") or "").strip().upper()
        if not sku:
            continue
        totals[sku] = totals.get(sku, 0) + 1
    return [{"sku": s, "qty": q} for s, q in sorted(totals.items())]


def extract_parcels(order: dict) -> dict:
    """Parcel handles for one order: package ids, plus carrier tracking numbers
    when the payload carries them.

    `package_id` is always present and is what the bot fetches waybills by, so it
    is the reliable handle. A carrier `tracking_number` is better for talking to
    the courier but TikTok does not always populate it at this stage, so it is
    recorded opportunistically and never required.
    """
    package_ids = sorted({
        str(p.get("id") or "").strip()
        for p in (order.get("packages") or [])
        if str(p.get("id") or "").strip()
    })
    tracking = sorted({
        str(src.get("tracking_number") or "").strip()
        for src in ((order.get("line_items") or []) + (order.get("packages") or []))
        if str(src.get("tracking_number") or "").strip()
    })
    couriers = sorted({
        str(i.get("shipping_provider_name") or "").strip()
        for i in (order.get("line_items") or [])
        if str(i.get("shipping_provider_name") or "").strip()
    })
    return {"package_ids": package_ids, "tracking_numbers": tracking,
            "couriers": couriers}


def load(path: Path | str = _PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        # Never let a corrupt cache stop a waybill: labels are the bot's job,
        # this file is bookkeeping. Report and carry on with an empty view.
        print(f"  ⚠ order_items.json tidak terbaca ({e}); mulai dari kosong")
        return {}


def save(state: dict, path: Path | str = _PATH) -> None:
    path = Path(path)
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)


def record(orders: list[dict], state: dict | None = None,
           now: datetime | None = None) -> dict:
    """Upsert every order's items and parcel handles, and return the state.

    Takes the FULL pending list, not just the unprocessed packages: goods leave
    the shelf when the order is picked, not when its waybill finally prints, and
    a run reporting "0 new" would otherwise record nothing at all.

    `first_seen` never drifts forward — it is when the goods started moving.
    """
    state = dict(state or {})
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for order in orders or []:
        oid = str(order.get("id") or "").strip()
        if not oid:
            continue
        items = extract(order)
        parcels = extract_parcels(order)
        prev = state.get(oid) or {}
        # Union the parcel handles: an order can gain a package between runs, and
        # replacing the list would drop the earlier parcel — losing the proof of
        # a shipment that already went out.
        package_ids = sorted(set(prev.get("package_ids") or []) | set(parcels["package_ids"]))
        tracking = sorted(set(prev.get("tracking_numbers") or []) | set(parcels["tracking_numbers"]))
        couriers = sorted(set(prev.get("couriers") or []) | set(parcels["couriers"]))
        state[oid] = {
            "order_status": order.get("status") or prev.get("order_status") or "",
            "first_seen": prev.get("first_seen") or stamp,
            "last_seen": stamp,
            "items": items or prev.get("items") or [],
            "pending_items": not items and not prev.get("items"),
            "package_ids": package_ids,
            "tracking_numbers": tracking,
            "couriers": couriers,
        }
    return state


def summarize(state: dict) -> dict[str, int]:
    """Total qty per SKU across every recorded order — the in-flight position."""
    totals: dict[str, int] = {}
    for rec in (state or {}).values():
        for line in rec.get("items") or []:
            sku = str(line.get("sku") or "")
            try:
                qty = int(line.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if sku and qty:
                totals[sku] = totals.get(sku, 0) + qty
    return totals
