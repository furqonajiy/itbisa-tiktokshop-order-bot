"""
main.py
-------
The entry point. Run this file to do one full processing cycle.

What this script does, in order:
  1. Load the list of order IDs we already processed.
  2. Ask TikTok Shop for orders in AWAITING_SHIPMENT or AWAITING_COLLECTION.
  3. Filter out the ones we already handled.
  4. Stop if there are too many (safety check).
  5. For each new order:
     a. If AWAITING_SHIPMENT, call ship_order() to mark it ready to ship.
        This moves the order to AWAITING_COLLECTION and triggers label
        availability.
     b. Fetch the shipping label PDF (with retries while TikTok Shop makes
        it available).
     c. Convert PDF to PNG, send to Telegram, mark as processed only AFTER
        Telegram confirms delivery.
  6. Save the updated state file so future runs remember.
  7. Send a heartbeat summary to Telegram so the employee knows the bot
     is alive, even if there were no orders this run.

The GitHub Actions workflow runs this script on a schedule.
"""

import sys
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
        # This case needs human attention because the bot cannot recover by itself.
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
    """The actual run logic. Separated so run() can wrap it in error handling."""

    # STEP 1: Load the dictionary of orders we already processed.
    processed = state_manager.load()
    print(f"Loaded state: {len(processed)} previously processed orders remembered")

    # STEP 2: Fetch all orders that need a label printed.
    print("Fetching pending orders from TikTok Shop...")
    orders = tiktokshop_client.get_pending_orders()
    print(f"TikTok Shop returned {len(orders)} pending orders")

    # STEP 3: Filter out orders we already processed in a previous run,
    # then sort them by order_id ascending so Telegram receives labels in a
    # stable oldest-to-newest order.
    new_orders = sorted(
        (o for o in orders if o["order_id"] not in processed),
        key=lambda o: str(o["order_id"]),
    )
    print(f"Of those, {len(new_orders)} are new and need processing")

    # STEP 4: If there are no new orders, send a heartbeat and exit.
    if not new_orders:
        summary = telegram_sender.build_summary(_now_jakarta_hhmm(), 0, 0)
        telegram_sender.send_summary(summary)
        print(f"Sent heartbeat: {summary}")
        return

    # STEP 5: Safety check. If we suddenly see too many orders, something is
    # probably wrong (e.g. state file got deleted). Stop and alert instead of
    # flooding the employee with hundreds of Telegram messages.
    if len(new_orders) > config.MAX_ORDERS_PER_RUN:
        warning = telegram_sender.build_safety_stop_message(
            _now_jakarta_hhmm(), len(new_orders), config.MAX_ORDERS_PER_RUN
        )
        telegram_sender.send_summary(warning)
        print(warning)
        sys.exit(1)

    # STEP 6: Process each new order one at a time.
    success_count = 0
    skipped_count = 0
    for order in new_orders:
        order_id = order["order_id"]
        order_status = order.get("order_status", "UNKNOWN")
        print(f"\nProcessing order {order_id} (status: {order_status})...")

        # STEP 6a: If the order is still awaiting shipment, tell TikTok Shop
        # the order is ready to ship first. After that, the label should become
        # available for download.
        if order_status == "AWAITING_SHIPMENT":
            try:
                print(f"  Marking {order_id} as ready to ship...")
                tiktokshop_client.ship_order(order_id)
                print("  Ready-to-ship succeeded. Label should be available soon.")
            except Exception as e:
                print(f"  ✗ Failed to mark {order_id} ready to ship: {e}")
                print("    Will retry next run.")
                skipped_count += 1
                continue

        # STEP 6b: Get the shipping label PDF from TikTok Shop.
        pdf_bytes = tiktokshop_client.get_shipping_label_pdf(order_id)
        if pdf_bytes is None:
            print(f"  Skipping {order_id} (label not ready). Will retry next run.")
            skipped_count += 1
            continue

        # STEP 6c: Convert the PDF to one or more PNG images.
        png_pages = label_processor.pdf_to_pngs(pdf_bytes)
        print(f"  Rendered {len(png_pages)} label page(s) from PDF")

        # STEP 6d: Build the caption and send all pages to Telegram.
        caption = telegram_sender.build_caption(order)
        delivered = telegram_sender.send_label(png_pages, caption)

        # STEP 6e: Only mark as processed if Telegram confirmed delivery.
        if delivered:
            processed[order_id] = state_manager.now_iso()
            success_count += 1
            print("  ✓ Sent to Telegram and marked as processed")
        else:
            print("  ✗ Telegram delivery failed. Will retry next run.")
            skipped_count += 1

    # STEP 7: Save the updated state file so the next run remembers what we did.
    state_manager.save(processed)
    print("\nState saved.")

    # STEP 8: Send a summary heartbeat so the employee knows what happened.
    summary = telegram_sender.build_summary(
        _now_jakarta_hhmm(), success_count, skipped_count
    )
    telegram_sender.send_summary(summary)

    # STEP 9: Print a summary so the GitHub Actions log is easy to scan.
    print("=" * 60)
    print(f"Run complete: {success_count} sent, {skipped_count} skipped")
    print("=" * 60)


if __name__ == "__main__":
    run()
