"""
main.py
-------
Entry point. Run this file to do one full processing cycle.

The flow:
  1. Load the set of package_ids we already processed in previous runs.
  2. Fetch all orders in AWAITING_SHIPMENT or AWAITING_COLLECTION.
  3. Extract package_ids from those orders (pairing each with its source
     order so we can build the Telegram caption later).
  4. Drop packages we've already processed.
  5. Safety-cap check so we never flood Telegram if something is wrong.
  6. Batch-ship every package whose source order is still AWAITING_SHIPMENT.
  7. For each new package: download waybill PDF -> convert to PNG(s) -> send
     to Telegram -> mark processed only AFTER Telegram confirms delivery.
  8. Save state.
  9. Send a heartbeat summary so the employee knows the bot ran.

We track package_id (not order_id) because each package gets its own waybill
and its own Telegram send. An order with multiple packages produces multiple
Telegram messages.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

from src import (
    config,
    label_processor,
    state_manager,
    telegram_sender,
    tiktokshop_client,
)

JAKARTA_TZ = timezone(timedelta(hours=7))


def _now_jakarta_hhmm():
    """Returns the current Jakarta time as a HH:MM string."""
    return datetime.now(JAKARTA_TZ).strftime("%H:%M")


def run():
    """Runs one full cycle. Forwards any error to Telegram before exiting."""

    print("=" * 60)
    print("ITBisa TikTok Shop Order Bot - starting run")
    print("=" * 60)

    try:
        _do_run()
    except Exception as e:
        alert = f"❌ {_now_jakarta_hhmm()} - Error bot TikTok Shop: {e}"
        telegram_sender.send_summary(alert)
        print(f"\n{alert}")
        sys.exit(1)


def _do_run():
    """The actual run. Kept separate so run() can wrap it cleanly."""

    processed = state_manager.load()
    print(f"Loaded state: {len(processed)} previously processed packages")

    print("Fetching pending orders from TikTok Shop...")
    orders = tiktokshop_client.get_pending_orders()
    print(f"TikTok Shop returned {len(orders)} pending orders")

    all_jobs = _extract_package_jobs(orders)
    print(f"Extracted {len(all_jobs)} package(s) from those orders")

    new_jobs = sorted(
        (job for job in all_jobs if job["package_id"] not in processed),
        key=lambda job: job["package_id"],
    )
    print(f"Of those, {len(new_jobs)} are new and need processing")

    # Heartbeat if nothing to do.
    if not new_jobs:
        summary = telegram_sender.build_summary(_now_jakarta_hhmm(), 0, 0)
        telegram_sender.send_summary(summary)
        print(f"Sent heartbeat: {summary}")
        return

    # Safety cap: if something looks wildly off, stop before flooding Telegram.
    if len(new_jobs) > config.MAX_ORDERS_PER_RUN:
        warning = telegram_sender.build_safety_stop_message(
            _now_jakarta_hhmm(), len(new_jobs), config.MAX_ORDERS_PER_RUN
        )
        telegram_sender.send_summary(warning)
        print(warning)
        sys.exit(1)

    # Batch-ship packages whose source order is still AWAITING_SHIPMENT.
    # Packages from AWAITING_COLLECTION orders are already shipped and only
    # need waybill generation.
    packages_to_ship = [
        job["package_id"] for job in new_jobs
        if job["order"]["status"] == "AWAITING_SHIPMENT"
    ]
    if packages_to_ship:
        print(f"Batch-shipping {len(packages_to_ship)} package(s) in one call...")
        try:
            tiktokshop_client.ship_packages(packages_to_ship)
            print("Batch ship succeeded.")
            time.sleep(3)  # Let TikTok Shop start generating waybills.
        except Exception as e:
            # Best-effort: already-shipped packages can still get waybills,
            # and anything that didn't really ship will return "not ready"
            # and retry on the next scheduled run.
            print(f"Batch ship failed (will continue best-effort): {e}")
    else:
        print("No packages need shipping; all are already AWAITING_COLLECTION.")

    # Process each package one at a time.
    success_count = 0
    skipped_count = 0
    for job in new_jobs:
        package_id = job["package_id"]
        order = job["order"]
        print(f"\nProcessing package {package_id} (order {order['id']})...")

        pdf_bytes = tiktokshop_client.get_waybill_pdf(package_id)
        if pdf_bytes is None:
            print(f"  Skipping {package_id} (waybill not ready). Will retry next run.")
            skipped_count += 1
            continue

        png_pages = label_processor.pdf_to_pngs(pdf_bytes)
        print(f"  Rendered {len(png_pages)} waybill page(s) from PDF")

        caption = telegram_sender.build_caption(order)
        delivered = telegram_sender.send_label(png_pages, caption)

        # Only mark and save processed AFTER Telegram confirms delivery.
        # Save immediately after each successful label so partial progress
        # survives even if a later package crashes before the final save.
        if delivered:
            processed[package_id] = state_manager.now_iso()
            state_manager.save(processed)
            success_count += 1
            print("  ✓ Sent to Telegram, saved state, and marked as processed")
        else:
            print("  ✗ Telegram delivery failed. Will retry next run.")
            skipped_count += 1

    print("\nState saved.")

    summary = telegram_sender.build_summary(
        _now_jakarta_hhmm(), success_count, skipped_count
    )
    telegram_sender.send_summary(summary)
    print("=" * 60)
    print(f"Run complete: {success_count} sent, {skipped_count} skipped")
    print("=" * 60)


def _extract_package_jobs(orders):
    """Produces one job per unique package_id, paired with its source order.

    Caption-building needs order-level data (id, line_items, couriers), so we
    keep a reference to the source order for each package.
    """
    jobs = []
    seen = set()

    for order in orders:
        for package in order["packages"]:
            package_id = package["id"]
            if package_id in seen:
                continue
            seen.add(package_id)
            jobs.append({"package_id": package_id, "order": order})

    return jobs


if __name__ == "__main__":
    run()