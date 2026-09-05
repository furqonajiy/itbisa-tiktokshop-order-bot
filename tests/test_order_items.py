"""What each order contained and which parcel it left in — the record that lets
an opname tell a write-off from goods already packed for shipment."""
from datetime import datetime, timezone

from src import order_items


def _order(oid, skus, packages=None, status="AWAITING_SHIPMENT", tracking=None):
    """TikTok returns ONE line_items row per UNIT, so `skus` is a list of rows."""
    items = [{"seller_sku": s, "shipping_provider_name": "JNE"} for s in skus]
    if tracking:
        for it in items:
            it["tracking_number"] = tracking
    return {"id": oid, "status": status, "line_items": items,
            "packages": [{"id": p} for p in (packages or [])]}


def test_qty_is_the_row_count_not_a_quantity_field():
    # TikTok returns one line_items entry PER UNIT — telegram_sender.build_caption
    # already counts rows. Reading a "quantity" key here would silently record 1
    # for an order of 50.
    got = order_items.extract(_order("O1", ["ITBISA-A"] * 50 + ["ITBISA-B"] * 3))
    assert got == [{"sku": "ITBISA-A", "qty": 50}, {"sku": "ITBISA-B", "qty": 3}]


def test_sku_is_normalized_and_blanks_dropped():
    got = order_items.extract({"line_items": [
        {"seller_sku": " itbisa-relay-12v-5pin-blue "},
        {"seller_sku": ""},
        {"seller_sku": None},
    ]})
    assert got == [{"sku": "ITBISA-RELAY-12V-5PIN-BLUE", "qty": 1}]


def test_parcel_handles_collect_packages_tracking_and_couriers():
    o = _order("O1", ["ITBISA-A"], packages=["P2", "P1"], tracking="JX99")
    got = order_items.extract_parcels(o)
    assert got["package_ids"] == ["P1", "P2"]        # sorted, deduped
    assert got["tracking_numbers"] == ["JX99"]
    assert got["couriers"] == ["JNE"]


def test_parcels_tolerate_a_payload_with_no_tracking_number():
    # package_id is always present and is what the bot fetches waybills by; a
    # carrier tracking number is recorded opportunistically and never required.
    got = order_items.extract_parcels(_order("O1", ["ITBISA-A"], packages=["P1"]))
    assert got["package_ids"] == ["P1"] and got["tracking_numbers"] == []


def test_record_captures_every_pending_order_and_keeps_first_seen():
    first = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    s = order_items.record([_order("O1", ["ITBISA-A"], packages=["P1"])], now=first)
    s = order_items.record([_order("O1", ["ITBISA-A"], packages=["P1"],
                                   status="AWAITING_COLLECTION")], s, now=later)
    assert len(s) == 1
    assert s["O1"]["first_seen"] == first.isoformat()
    assert s["O1"]["last_seen"] == later.isoformat()
    assert s["O1"]["order_status"] == "AWAITING_COLLECTION"


def test_a_new_package_is_added_never_replaces_the_earlier_one():
    # An order can gain a package between runs. Replacing the list would drop the
    # earlier parcel — losing the proof of a shipment that already went out.
    s = order_items.record([_order("O1", ["ITBISA-A"], packages=["P1"])])
    s = order_items.record([_order("O1", ["ITBISA-A"], packages=["P2"],
                                   tracking="JX7")], s)
    assert s["O1"]["package_ids"] == ["P1", "P2"]
    assert s["O1"]["tracking_numbers"] == ["JX7"]


def test_empty_line_items_is_flagged_not_dropped():
    s = order_items.record([_order("O1", [])])
    assert s["O1"]["items"] == [] and s["O1"]["pending_items"] is True
    s = order_items.record([_order("O1", ["ITBISA-A"])], s)
    assert s["O1"]["items"] == [{"sku": "ITBISA-A", "qty": 1}]
    assert s["O1"]["pending_items"] is False


def test_summarize_totals_the_in_flight_position():
    s = order_items.record([
        _order("O1", ["ITBISA-A"] * 30),
        _order("O2", ["ITBISA-A"] * 5 + ["ITBISA-B"] * 2),
    ])
    assert order_items.summarize(s) == {"ITBISA-A": 35, "ITBISA-B": 2}


def test_load_survives_a_corrupt_file_and_roundtrips(tmp_path):
    p = tmp_path / "order_items.json"
    p.write_text("{not json", encoding="utf-8")
    assert order_items.load(p) == {}
    s = order_items.record([_order("O1", ["ITBISA-A"], packages=["P1"])])
    order_items.save(s, p)
    assert order_items.load(p) == s
