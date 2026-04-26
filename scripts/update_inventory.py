"""
update_inventory.py
-------------------
Updates TikTok Shop inventory from an Excel file.

What this script does:
  1. Reads an Excel file with two columns: SKU and Stock.
  2. Paginates POST /product/202502/products/search. The 202502 search
     response includes the full SKU detail inline (id, seller_sku,
     inventory[]), so one paginated call gives us everything we need
     to build the SKU lookup. No per-product detail call required.
  3. Builds a lookup map: seller_sku -> [list of (product_id, sku_id,
     warehouse_id)]. The list matters: shared SKUs like packaging or
     accessories live on many products, and we want to update ALL of
     them when the warehouse sets a new stock count.
  4. For each SKU in your Excel, calls PUT update inventory once per
     product that carries that SKU.
  5. Prints a summary of what succeeded, failed, or was skipped.

APIs used:
  POST /product/202502/products/search                              (read)
  PUT  /product/202309/products/{product_id}/inventory/update       (write)

  Both are NEW (post-September 2023) TikTok Shop Open API endpoints.
  Legacy /api/products/* endpoints are not used. The search endpoint is
  intentionally on 202502 because that version returns full SKU detail
  in the search response, eliminating the need for per-product fetches.
  The signed `version` query param must match the path version, so we
  override the client default (202309) for the search call only.

Excel format expected:
  - Row 1: headers "SKU" and "Stock" (column order matters, header text
    is ignored)
  - Subsequent rows: one SKU per row
  Example:
    SKU                              | Stock
    ITBISA-LED-5MM-RED               | 100
    ITBISA-BUBBLE-WRAP               | 0    ← will zero on every product
                                              that uses this SKU

Usage:
  python scripts/update_inventory.py path/to/inventory.xlsx

Important notes:
  - SKUs not found in the TikTok Shop catalog are skipped with a warning.
    The run continues for other SKUs.
  - When one Excel SKU appears on N products (e.g. shared packaging),
    we issue N PUT calls, one per product. The progress output shows
    the product_id for each call so you can audit what was touched.
  - Update Inventory is a per-product call (not bulk across products),
    so the per-product fan-out is unavoidable. We delay between calls
    to stay polite to TikTok's documented 20 QPS cap.
  - The warehouse_id is auto-detected from each SKU's existing inventory
    record. Multi-warehouse shops will only have the FIRST warehouse
    updated; revisit this if the warehouse later moves to having multiple.
"""

import sys
import time
from pathlib import Path

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config, tiktokshop_client


# Polite delay between PUT calls. TikTok docs mention 20 QPS as a cap;
# we stay well below that to leave room for retries if needed.
_DELAY_BETWEEN_CALLS_SECONDS = 1.0

# TikTok's documented max for product search is 100. Larger pages mean
# fewer paginated round-trips during catalog read.
_SEARCH_PAGE_SIZE = 100

# Versions per endpoint. See module docstring for why these differ.
_SEARCH_API_VERSION = "202502"
_INVENTORY_API_VERSION = "202309"


def main():
    """Entry point: read Excel, fetch mappings, update each SKU."""

    if len(sys.argv) < 2:
        print("Usage: python scripts/update_inventory.py path/to/inventory.xlsx")
        sys.exit(1)

    excel_path = Path(sys.argv[1])
    if not excel_path.exists():
        print(f"✗ File not found: {excel_path}")
        sys.exit(1)

    print("=" * 60)
    print("TikTok Shop Inventory Update")
    print("=" * 60)
    print(f"App key:    {config.TIKTOKSHOP_APP_KEY}")
    print(f"Shop ID:    {config.TIKTOKSHOP_SHOP_ID}")
    print(f"Excel file: {excel_path}")
    print()

    # STEP 1: Read the Excel file into a dict of SKU -> desired stock.
    print("[1/4] Reading Excel file...")
    desired_stock = _read_excel(excel_path)
    print(f"  Found {len(desired_stock)} unique SKUs to process")
    print()

    # STEP 2: Walk the entire catalog in one paginated call series and
    # build the seller_sku -> [(product_id, sku_id, warehouse_id), ...] map.
    print("[2/4] Fetching product catalog from TikTok Shop...")
    sku_to_targets = _build_sku_mapping()
    total_targets = sum(len(t) for t in sku_to_targets.values())
    print(
        f"  Mapped {len(sku_to_targets)} unique seller_skus "
        f"across {total_targets} product/sku rows"
    )
    print()

    # STEP 3: Match each Excel SKU to its TikTok Shop ids. One Excel
    # row may expand into multiple update jobs if the SKU is shared
    # across products (very common for packaging/accessory SKUs).
    print("[3/4] Matching Excel SKUs to TikTok Shop SKUs...")
    matched = []
    missing = []
    for sku, stock in desired_stock.items():
        targets = sku_to_targets.get(sku, [])
        if not targets:
            missing.append(sku)
            continue
        for product_id, sku_id, warehouse_id in targets:
            matched.append((sku, stock, product_id, sku_id, warehouse_id))

    matched_excel_skus = len(desired_stock) - len(missing)
    print(f"  Excel SKUs matched: {matched_excel_skus}")
    print(f"  Excel SKUs missing: {len(missing)}")
    print(f"  Total update calls planned: {len(matched)}")
    if missing:
        print("  Missing SKUs (not found on TikTok Shop, will be skipped):")
        for sku in missing:
            print(f"    - {sku}")

    # Show fan-out so the operator notices when a SKU touches many products.
    fanned_out = [
        (sku, len(sku_to_targets[sku]))
        for sku in desired_stock
        if len(sku_to_targets.get(sku, [])) > 1
    ]
    if fanned_out:
        print("  SKUs that exist on multiple products (each will be updated):")
        for sku, count in fanned_out:
            print(f"    - {sku}: {count} products")
    print()

    if not matched:
        print("Nothing to update. Exiting.")
        return

    # STEP 4: Update each matched SKU's stock on TikTok Shop.
    print(f"[4/4] Issuing {len(matched)} update call(s)...")
    success_count = 0
    failed = []
    for sku, stock, product_id, sku_id, warehouse_id in matched:
        label = f"{sku} (product {product_id})"
        try:
            _update_stock(product_id, sku_id, warehouse_id, stock)
            success_count += 1
            print(f"  ✓ {label}: set stock to {stock}")
        except Exception as e:
            failed.append((label, str(e)))
            print(f"  ✗ {label}: failed - {e}")

        time.sleep(_DELAY_BETWEEN_CALLS_SECONDS)

    print()
    print("=" * 60)
    print(
        f"Done. {success_count} updated, {len(failed)} failed, "
        f"{len(missing)} skipped."
    )
    print("=" * 60)

    if failed:
        print("\nFailed updates (check these manually):")
        for label, err in failed:
            print(f"  - {label}: {err}")


# ============================================================
# Excel reading
# ============================================================

def _read_excel(path):
    """
    Reads the Excel file and returns a dict of SKU -> stock.

    Expects the first row to be headers and the first two columns to
    contain SKU and Stock. Rows where SKU is empty are skipped.
    """

    workbook = openpyxl.load_workbook(path, data_only=True)
    sheet = workbook.active

    result = {}
    for row_num, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if not row or len(row) < 2:
            continue

        sku_raw = row[0]
        stock_raw = row[1]

        if sku_raw is None or str(sku_raw).strip() == "":
            continue

        sku = str(sku_raw).strip()

        try:
            stock = int(stock_raw)
        except (TypeError, ValueError):
            print(f"  Row {row_num}: invalid stock value '{stock_raw}' for SKU '{sku}', skipping")
            continue

        if stock < 0:
            print(f"  Row {row_num}: negative stock '{stock}' for SKU '{sku}', skipping")
            continue

        # If the same SKU appears on more than one Excel row, the last
        # value wins. We warn so the operator notices the duplicate.
        if sku in result and result[sku] != stock:
            print(
                f"  Row {row_num}: SKU '{sku}' was already set to "
                f"{result[sku]} on an earlier row; overwriting with {stock}"
            )
        result[sku] = stock

    return result


# ============================================================
# TikTok Shop product catalog fetching (NEW API 202502 search)
# ============================================================

def _build_sku_mapping():
    """
    Returns dict: seller_sku -> [list of (product_id, sku_id, warehouse_id)].

    Why a list and not a single tuple:
      Some SKUs (packaging, accessories, common components) live on many
      products. When the warehouse says "ITBISA-BUBBLE-WRAP = 0", they
      mean every product that uses that SKU goes to 0, not just one
      arbitrary product. The list captures every (product, sku, warehouse)
      target so the caller can update all of them.

    Implementation note:
      The 202502 search response embeds skus[] inline, so this function
      reads the entire catalog in one paginated walk and never needs
      a per-product detail call. ~2 API calls for 118 products vs.
      ~120 calls under the old 202309 detail-per-product flow.
    """

    sku_to_targets = {}
    page_token = None
    page_num = 0
    products_seen = 0

    while True:
        page_num += 1
        extra_query = {
            "page_size": str(_SEARCH_PAGE_SIZE),
            "version": _SEARCH_API_VERSION,
        }
        if page_token:
            extra_query["page_token"] = page_token

        # Body intentionally empty — matches your working curl. Adding a
        # status filter here is supported but unnecessary; we want
        # everything in the catalog so the warehouse can update any SKU.
        body = {}
        response = tiktokshop_client._call_signed(
            "POST",
            "/product/202502/products/search",
            extra_query=extra_query,
            body=body,
        )
        tiktokshop_client._check_ok(response, context="product search")

        payload = response.json()["data"]
        products = payload.get("products") or []
        products_seen += len(products)

        if page_num == 1:
            total = payload.get("total_count", "?")
            print(f"  Catalog reports {total} total products")

        for product in products:
            product_id = product["id"]
            for sku in product.get("skus") or []:
                seller_sku = (sku.get("seller_sku") or "").strip()
                if not seller_sku:
                    continue

                sku_id = sku.get("id")
                inventories = sku.get("inventory") or []
                if not sku_id or not inventories:
                    # Without an existing inventory record we don't know
                    # which warehouse_id to target, so skip.
                    continue

                warehouse_id = inventories[0].get("warehouse_id")
                if not warehouse_id:
                    continue

                sku_to_targets.setdefault(seller_sku, []).append(
                    (product_id, sku_id, warehouse_id)
                )

        page_token = payload.get("next_page_token") or ""
        print(f"  Page {page_num}: {len(products)} products (running total: {products_seen})")
        if not page_token:
            break

        time.sleep(0.3)  # Be polite between pages.

    return sku_to_targets


# ============================================================
# Inventory update (NEW API 202309)
# ============================================================

def _update_stock(product_id, sku_id, warehouse_id, new_stock):
    """
    PUT /product/202309/products/{product_id}/inventory/update.

    Per-product endpoint: you can update multiple SKUs of the SAME
    product in one call, but cannot mix SKUs from different products.
    For Excel-driven updates, where there's no guarantee that adjacent
    rows belong to the same product, we just do one SKU per call. That
    keeps progress output simple and gives clean per-SKU error attribution.

    Args:
      product_id:   TikTok product id (string).
      sku_id:       TikTok SKU id (string).
      warehouse_id: TikTok warehouse id (string).
      new_stock:    Absolute stock count to set (int).

    Raises:
      RuntimeError if TikTok returns an error or per-SKU failure.
    """

    path = f"/product/{_INVENTORY_API_VERSION}/products/{product_id}/inventory/update"
    body = {
        "skus": [
            {
                "id": sku_id,
                "inventory": [
                    {"warehouse_id": warehouse_id, "quantity": new_stock},
                ],
            },
        ],
    }

    response = tiktokshop_client._call_signed("POST", path, body=body)
    tiktokshop_client._check_ok(response, context=f"update inventory sku={sku_id}")

    # Even with code=0, TikTok may report per-SKU failures inside data.
    # Field name has shifted across spec revisions; check both.
    data = response.json().get("data") or {}
    failures = data.get("errors") or data.get("failed_skus") or []
    if failures:
        raise RuntimeError(f"per-sku failures: {failures}")


if __name__ == "__main__":
    main()