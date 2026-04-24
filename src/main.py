"""
main.py
-------
The entry point. Run this file to do one full processing cycle.

The flow, end to end:
  1. Load the set of package ids we already processed in previous runs.
  2. Fetch all TikTok Shop orders in AWAITING_SHIPMENT or AWAITING_COLLECTION.
     (tiktokshop_client also fetches the shop cipher it needs first.)
  3. Extract all package ids from those orders, pairing each with its source
     order so we can build the Telegram caption later.
  4. Drop packages we've already processed.
  5. Safety-cap check. If something looks wildly off, stop and alert.
  6. Batch-ship every package whose order is still AWAITING_SHIPMENT. This
     is the TikTok Shop equivalent of "arrange shipment" and is done in a
     single API call for efficiency.
  7. For each new package:
       a. Generate and download the waybill PDF.
       b. Convert the PDF to PNG image(s).
       c. Send the image(s) to Telegram with a caption built from the source
          order (order id, courier, grouped SKUs).
       d. Mark the package as processed only AFTER Telegram confirms delivery.
  8. Save the updated state file.
  9. Send a heartbeat summary so the employee knows the bot ran.

State granularity:
  We track package_id, not order_id, because each package gets its own
  waybill and its own Telegram send. An order with multiple packages will
  produce multiple Telegram messages, one per package.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

from src import (
    config,
    label_processor,
    state_manager,
    telegram_sender,
    tiktokshop_auth,
    tiktokshop_client,
)


# Jakarta is UTC+7. We use this to format the time in summaries.
JAKARTA_TZ = timezone(timedelta(hours=7))


def _now_jakarta_hhmm():
    """Returns the current time in Jakarta as a HH:MM string."""
    return datetime.now(JAKARTA_TZ).strftime("%H:%M")


def run():
    """Runs one full cycle. Returns nothing. Prints progress to stdout."""

    print("=" * 60)
    print("ITBisa TikTok Shop Order Bot - starting run")
    print("=" * 60)

    # STEP 0: Wrap the whole run in a try block so we can catch the one
    # error that requires a human response: an expired refresh_token.
    try:
        _do_run()
    except tiktokshop_auth.RefreshTokenExpiredError as e:
        alert = (
            f"🔐 {_now_jakarta_hhmm()} - Otorisasi TikTok Shop kadaluarsa. "
            f"Mohon otorisasi ulang aplikasi di TikTok Shop Open Platform, "
            f"lalu update file data/tiktokshop_tokens.json dengan token baru."
        )
        telegram_sender.send_summary(alert)
        print(f"\n{alert}")
        print(f"Details: {e}")
        sys.exit(1)


def _do_run():
    """The actual run logic. Kept separate so run() can wrap it cleanly."""

    # STEP 1: Load the package-level state.
    processed = state_manager.load()
    print(f"Loaded state: {len(processed)} previously processed packages remembered")

    # STEP 2: Fetch all pending orders (fetches shop cipher internally on demand).
    print("Fetching pending orders from TikTok Shop...")
    orders = tiktokshop_client.get_pending_orders()
    print(f"TikTok Shop returned {len(orders)} pending orders")

    # STEP 3: Extract every package from every order, remembering which order
    # each package belongs to so we can build captions later.
    all_package_jobs = _extract_package_jobs(orders)
    print(f"Extracted {len(all_package_jobs)} package(s) from those orders")

    # STEP 4: Drop packages we already processed, then sort for stable order.
    new_jobs = sorted(
        (job for job in all_package_jobs if job["package_id"] not in processed),
        key=lambda job: job["package_id"],
    )
    print(f"Of those, {len(new_jobs)} are new and need processing")

    # STEP 5: Heartbeat if nothing to do.
    if not new_jobs:
        summary = telegram_sender.build_summary(_now_jakarta_hhmm(), 0, 0)
        telegram_sender.send_summary(summary)
        print(f"Sent heartbeat: {summary}")
        return

    # STEP 6: Safety cap so we never flood Telegram if something is wrong.
    if len(new_jobs) > config.MAX_ORDERS_PER_RUN:
        warning = telegram_sender.build_safety_stop_message(
            _now_jakarta_hhmm(), len(new_jobs), config.MAX_ORDERS_PER_RUN
        )
        telegram_sender.send_summary(warning)
        print(warning)
        sys.exit(1)

    # STEP 7: Batch-ship the packages whose source order is still
    # AWAITING_SHIPMENT. Packages from AWAITING_COLLECTION orders are already
    # shipped and only need waybill generation.
    packages_to_ship = [
        job["package_id"]
        for job in new_jobs
        if _order_status(job["order"]) == "AWAITING_SHIPMENT"
    ]
    if packages_to_ship:
        print(f"Batch-shipping {len(packages_to_ship)} package(s) in one call...")
        try:
            tiktokshop_client.ship_packages(packages_to_ship)
            print("Batch ship succeeded.")
            # Small pause to let TikTok Shop start generating waybills.
            time.sleep(3)
        except Exception as e:
            # Best-effort: packages that were already AWAITING_COLLECTION can
            # still get their waybills in the loop below, and any that really
            # didn't ship will simply return "not ready" and retry next run.
            print(f"Batch ship failed (will continue best-effort): {e}")
    else:
        print("No packages need shipping; all are already AWAITING_COLLECTION.")

    # STEP 8: Process each package one at a time.
    success_count = 0
    skipped_count = 0
    for job in new_jobs:
        package_id = job["package_id"]
        source_order = job["order"]
        order_id = _extract_order_id(source_order)
        print(f"\nProcessing package {package_id} (order {order_id})...")

        # STEP 8a: Get the waybill PDF.
        pdf_bytes = tiktokshop_client.get_waybill_pdf(package_id)
        if pdf_bytes is None:
            print(f"  Skipping {package_id} (waybill not ready). Will retry next run.")
            skipped_count += 1
            continue

        # STEP 8b: PDF -> PNG(s).
        png_pages = label_processor.pdf_to_pngs(pdf_bytes)
        print(f"  Rendered {len(png_pages)} waybill page(s) from PDF")

        # STEP 8c: Build caption and send.
        caption = telegram_sender.build_caption(source_order)
        delivered = telegram_sender.send_label(png_pages, caption)

        # STEP 8d: Only mark processed when Telegram confirmed delivery.
        if delivered:
            processed[package_id] = state_manager.now_iso()
            success_count += 1
            print("  ✓ Sent to Telegram and marked as processed")
        else:
            print("  ✗ Telegram delivery failed. Will retry next run.")
            skipped_count += 1

    # STEP 9: Persist state so the next run remembers what we did.
    state_manager.save(processed)
    print("\nState saved.")

    # STEP 10: Send heartbeat + console summary.
    summary = telegram_sender.build_summary(
        _now_jakarta_hhmm(), success_count, skipped_count
    )
    telegram_sender.send_summary(summary)
    print("=" * 60)
    print(f"Run complete: {success_count} sent, {skipped_count} skipped")
    print("=" * 60)


# ============================================================
# Package extraction helpers
# ============================================================

def _extract_package_jobs(orders):
    """
    Walks every order and produces one job per unique package id.

    Each job carries both the package_id and a reference to the order that
    contained it, because caption-building needs order-level data (id,
    line items, couriers).
    """

    # STEP 1: Collect (package_id, source_order) pairs, first-seen wins.
    jobs = []
    seen_package_ids = set()

    for order in orders:
        for package_id in _find_package_ids_in_order(order):
            if package_id in seen_package_ids:
                continue
            seen_package_ids.add(package_id)
            jobs.append({
                "package_id": package_id,
                "order": order,
            })

    return jobs


def _find_package_ids_in_order(order):
    """
    Returns a sorted list of unique package ids nested anywhere inside the
    order object.

    We scan recursively because TikTok Shop's order payload can nest
    packages under different keys (packages[].id, package_id, packageIds,
    etc.) depending on endpoint and shop setup.
    """

    found = set()

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                # Direct single-id fields.
                if key in ("package_id", "packageId") and nested not in (None, ""):
                    found.add(str(nested))
                # Direct list-of-id fields.
                elif key in ("package_ids", "packageIds") and isinstance(nested, list):
                    for item in nested:
                        if item not in (None, ""):
                            found.add(str(item))
                # Recurse into everything else.
                else:
                    walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(order)
    return sorted(found)


def _order_status(order):
    """Returns the TikTok Shop order status string, or 'UNKNOWN'."""
    return (
            order.get("order_status")
            or order.get("status")
            or order.get("orderStatus")
            or "UNKNOWN"
    )


def _extract_order_id(order):
    """Returns the order id for display/logging purposes."""
    return (
            order.get("id")
            or order.get("order_id")
            or order.get("orderId")
            or "?"
    )


if __name__ == "__main__":
    run()