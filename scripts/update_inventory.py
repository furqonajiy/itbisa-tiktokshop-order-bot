"""
update_inventory.py
-------------------
Updates TikTok Shop inventory from an Excel file.

What this script does:
  1. Reads an Excel file with two columns: SKU and Stock.
  2. Fetches all products from TikTok Shop, including each SKU's id and
     warehouse_id (needed by the inventory endpoint).
  3. Builds a lookup map: seller_sku -> (product_id, sku_id, warehouse_id).
  4. For each SKU in your Excel, calls Update Inventory (NEW API 202309).
  5. Prints a summary of what succeeded, failed, or was skipped.

API used:
  PUT /product/202309/products/{product_id}/inventory/update
  This is the NEW (post-September 2023) TikTok Shop Open API. Legacy
  /api/products/* endpoints are not used.

Excel format expected:
  - Row 1: headers "SKU" and "Stock" (column order matters, header text
    is ignored)
  - Subsequent rows: one SKU per row
  Example:
    SKU                              | Stock
    ITBISA-LED-5MM-RED               | 100
    ITBISA-LED-5MM-GREEN             | 75

Usage:
  python scripts/update_inventory.py path/to/inventory.xlsx

Important notes:
  - SKUs not found in the TikTok Shop catalog are skipped with a warning.
    The run continues for other SKUs.
  - Update Inventory is a per-product call (not bulk across products),
    so we issue one PUT per SKU. We delay between calls to stay polite
    to TikTok's documented 20 QPS cap.
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


# Polite delay between calls. TikTok docs mention 20 QPS as a cap;
# we stay well below that to leave room for retries if needed.
_DELAY_BETWEEN_CALLS_SECONDS = 1.0

# TikTok's documented max for product search.
_SEARCH_PAGE_SIZE = 100


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
    print(f"  Found {len(desired_stock)} SKUs to process")
    print()

    # STEP 2: Fetch all products from TikTok Shop and build the
    # seller_sku -> (product_id, sku_id, warehouse_id) mapping. Slowest
    # step because TikTok requires one detail call per product.
    print("[2/4] Fetching product catalog from TikTok Shop...")
    sku_to_target = _build_sku_mapping()
    print(f"  Mapped {len(sku_to_target)} SKUs from TikTok Shop catalog")
    print()

    # STEP 3: Match each Excel SKU to its TikTok Shop ids.
    print("[3/4] Matching Excel SKUs to TikTok Shop SKUs...")
    matched = []
    missing = []
    for sku, stock in desired_stock.items():
        if sku in sku_to_target:
            product_id, sku_id, warehouse_id = sku_to_target[sku]
            matched.append((sku, stock, product_id, sku_id, warehouse_id))
        else:
            missing.append(sku)

    print(f"  Matched: {len(matched)}")
    print(f"  Missing: {len(missing)}")
    if missing:
        print("  These SKUs will be skipped (not found on TikTok Shop):")
        for sku in missing:
            print(f"    - {sku}")
    print()

    if not matched:
        print("Nothing to update. Exiting.")
        return

    # STEP 4: Update each matched SKU's stock on TikTok Shop.
    print(f"[4/4] Updating stock for {len(matched)} SKUs...")
    success_count = 0
    failed = []
    for sku, stock, product_id, sku_id, warehouse_id in matched:
        try:
            _update_stock(product_id, sku_id, warehouse_id, stock)
            success_count += 1
            print(f"  ✓ {sku}: set stock to {stock}")
        except Exception as e:
            failed.append((sku, str(e)))
            print(f"  ✗ {sku}: failed - {e}")

        time.sleep(_DELAY_BETWEEN_CALLS_SECONDS)

    print()
    print("=" * 60)
    print(f"Done. {success_count} updated, {len(failed)} failed, "
          f"{len(missing)} skipped.")
    print("=" * 60)

    if failed:
        print("\nFailed SKUs (check these manually):")
        for sku, err in failed:
            print(f"  - {sku}: {err}")


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

        result[sku] = stock

    return result


# ============================================================
# TikTok Shop product catalog fetching
# ============================================================

def _build_sku_mapping():
    """
    Fetches all products from TikTok Shop and returns a dict of
    seller_sku -> (product_id, sku_id, warehouse_id).

    TikTok's update_inventory endpoint requires the product_id (in URL),
    the sku_id, AND the warehouse_id — none of which are in your Excel —
    so we have to walk the catalog up front to look them all up.
    """

    # STEP 1: Get the full list of product_ids in this shop.
    product_ids = _fetch_all_product_ids()
    print(f"  Found {len(product_ids)} products total")

    if not product_ids:
        return {}

    # STEP 2: Fetch detail for each product to extract its SKU info.
    # TikTok 202309 has no batch detail endpoint (unlike Shopee's
    # get_item_base_info which takes 50 ids at once), so this is one
    # call per product. We rate limit between calls.
    sku_to_target = {}
    for index, product_id in enumerate(product_ids, start=1):
        if index == 1 or index % 10 == 0 or index == len(product_ids):
            print(f"  Fetching product detail {index}/{len(product_ids)}...")

        try:
            detail = _fetch_product_detail(product_id)
        except Exception as e:
            print(f"  ! Skipping product {product_id} (detail fetch failed): {e}")
            time.sleep(_DELAY_BETWEEN_CALLS_SECONDS)
            continue

        for sku in detail.get("skus", []):
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

            sku_to_target[seller_sku] = (product_id, sku_id, warehouse_id)

        time.sleep(_DELAY_BETWEEN_CALLS_SECONDS)

    return sku_to_target


def _fetch_all_product_ids():
    """
    Paginates POST /product/202309/products/search to collect every
    product_id in the shop. status=ALL returns active, draft, and
    inactive products so we don't miss anything the warehouse cares about.
    """

    path = "/product/202309/products/search"
    all_ids = []
    page_token = None

    while True:
        extra_query = {"page_size": str(_SEARCH_PAGE_SIZE)}
        if page_token:
            extra_query["page_token"] = page_token

        body = {"status": "ALL"}
        response = tiktokshop_client._call_signed(
            "POST", path, extra_query=extra_query, body=body,
        )
        tiktokshop_client._check_ok(response, context="product search")

        payload = response.json()["data"]
        for product in payload.get("products", []):
            all_ids.append(product["id"])

        page_token = payload.get("next_page_token", "")
        if not page_token:
            break

        time.sleep(0.3)  # Be polite between pages.

    return all_ids


def _fetch_product_detail(product_id):
    """
    GET /product/202309/products/{product_id}.

    Returns the raw product dict. Of interest to us: data.skus[], where
    each SKU has id, seller_sku, and inventory[{warehouse_id, quantity}].
    """

    path = f"/product/202309/products/{product_id}"
    response = tiktokshop_client._call_signed("GET", path)
    tiktokshop_client._check_ok(response, context=f"product detail {product_id}")
    return response.json()["data"]


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

    path = f"/product/202309/products/{product_id}/inventory/update"
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

    response = tiktokshop_client._call_signed("PUT", path, body=body)
    tiktokshop_client._check_ok(response, context=f"update inventory sku={sku_id}")

    # Even with code=0, TikTok may report per-SKU failures inside data.
    # Field name has shifted across spec revisions; check both.
    data = response.json().get("data") or {}
    failures = data.get("errors") or data.get("failed_skus") or []
    if failures:
        raise RuntimeError(f"per-sku failures: {failures}")


if __name__ == "__main__":
    main()