"""
update_inventory.py
-------------------
Updates TikTok Shop inventory from an Excel file, with pack-size variant
rebalancing.

Background:
  TikTok Shop caps a single buyer's purchase at 20 units per SKU. To sell
  larger quantities of small components (capacitors, LEDs, etc.), the shop
  publishes multiple SKU variants of the SAME product, each representing
  a bundle size:
    - ITBISA-KAPASITOR-ELCO-2200UF-25V         (1 piece per unit)
    - 20PCS-ITBISA-KAPASITOR-ELCO-2200UF-25V   (20 pieces per unit)
    - 500PCS-ITBISA-KAPASITOR-ELCO-2200UF-25V  (500 pieces per unit)

  This script translates ONE warehouse stock count (in actual pieces) into
  per-variant unit counts that, when multiplied out, sum back to the
  original piece count.

What the operator does:
  Provides an Excel sheet with two columns: SKU and Stock.
    - SKU should always be the BASE SKU (the 1-piece variant).
    - Stock is the absolute number of physical pieces in the warehouse.
    - Pack-size variants in Excel (e.g. "20PCS-...") are skipped with a
      warning; the base SKU drives the rebalance for all variants.

Allocation rule (per product):
  Given total_pieces P and N pack-size variants:
    1. share = P // N pieces per variant.
    2. units = share // multiplier (rounded down) for each variant.
    3. remainder = P - sum(units * multiplier).
    4. The SMALLEST pack size absorbs (remainder // smallest_multiplier)
       extra units. Anything below the smallest multiplier is lost (e.g.
       3 leftover pieces with no 1pc variant cannot be allocated).

  Example: P=3000, variants {1, 20, 500} on one product:
    share = 1000 pcs/variant
      1pc:   1000 // 1   = 1000 units (= 1000 pcs)
      20pc:  1000 // 20  =   50 units (= 1000 pcs)
      500pc: 1000 // 500 =    2 units (= 1000 pcs)
    Total represented = 3000, remainder = 0. ✓

  Example: P=100, variants {1, 20, 500}:
    share = 33 pcs/variant
      1pc:   33 units (= 33 pcs)
      20pc:  1  unit  (= 20 pcs)
      500pc: 0  units (= 0 pcs)
    Represented = 53, remainder = 47 → 47 extra units to 1pc.
    Final 1pc = 80, total = 80 + 20 + 0 = 100 pcs. ✓

Algorithm note for non-variant SKUs:
  An ordinary SKU (e.g. ITBISA-BUBBLE-WRAP) becomes a single "variant"
  with multiplier=1. The allocation reduces to: set its stock to the
  Excel value. So one code path handles both shapes.

APIs used:
  POST /product/202502/products/search                              (read)
  POST /product/202309/products/{product_id}/inventory/update       (write)

  Both are NEW (post-September 2023) TikTok Shop Open API endpoints.
  202502 search inlines full SKU detail (id, seller_sku, inventory[]),
  so one paginated walk replaces the legacy detail-per-product flow.
  The signed `version` query param must match the path version, so we
  override the client default (202309) for the search call only.

Per-product batching:
  All variants of a given base SKU live on the same product, so each
  rebalance is one PUT call carrying multiple SKUs in the body (TikTok
  allows multi-SKU update as long as all SKUs belong to one product).
  Compared to per-SKU calls, this cuts API volume by roughly the
  variant-count factor.

Excel format expected:
  Row 1: headers "SKU" and "Stock" (column order matters; header text
  is ignored).
  Subsequent rows: one base SKU per row.
    SKU                                | Stock
    ITBISA-KAPASITOR-ELCO-2200UF-25V   | 3000
    ITBISA-BUBBLE-WRAP                 | 0      (no variants → set as-is)
    20PCS-ITBISA-KAPASITOR-ELCO-...    | 50     (skipped with warning)

Usage:
  python scripts/update_inventory.py path/to/tiktokshop_inventory.xlsx
"""

import re
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

# Pack-size variant pattern: "<digits>PCS-<base_sku>".
# Match must be exact and case-sensitive. "20PCS-X" → base "X", multiplier 20.
# A SKU that doesn't match (or matches with multiplier 0) is treated as a
# non-variant with implied multiplier 1.
_VARIANT_PATTERN = re.compile(r"^(\d+)PCS-(.+)$")


def main():
    """Entry point: read Excel, rebalance pack-size variants, update each product."""

    if len(sys.argv) < 2:
        print("Usage: python scripts/update_inventory.py path/to/tiktokshop_inventory.xlsx")
        sys.exit(1)

    excel_path = Path(sys.argv[1])
    if not excel_path.exists():
        print(f"✗ File not found: {excel_path}")
        sys.exit(1)

    print("=" * 60)
    print("TikTok Shop Inventory Update (with pack-size rebalancing)")
    print("=" * 60)
    print(f"App key:    {config.TIKTOKSHOP_APP_KEY}")
    print(f"Shop ID:    {config.TIKTOKSHOP_SHOP_ID}")
    print(f"Excel file: {excel_path}")
    print()

    # STEP 1: Read the Excel file. Variant-pattern SKUs are filtered out
    # here; the base SKU drives all variant updates.
    print("[1/4] Reading Excel file...")
    desired_stock, skipped_variants = _read_excel(excel_path)
    print(f"  Found {len(desired_stock)} base SKU(s) to process")
    if skipped_variants:
        print(f"  Skipped {len(skipped_variants)} variant-pattern row(s):")
        for sku in skipped_variants:
            print(f"    - {sku}  (use base SKU instead; variants auto-rebalance)")
    print()

    # STEP 2: Walk the catalog and group SKUs by (product, base).
    print("[2/4] Fetching product catalog from TikTok Shop...")
    base_to_products = _build_base_sku_mapping()
    total_groupings = sum(len(p) for p in base_to_products.values())
    print(
        f"  Mapped {len(base_to_products)} base SKU(s) "
        f"across {total_groupings} product grouping(s)"
    )
    print()

    # STEP 3: Match Excel rows to catalog and compute per-variant allocations.
    print("[3/4] Matching Excel SKUs and computing allocations...")
    jobs = []
    missing = []
    for sku, stock in desired_stock.items():
        product_entries = base_to_products.get(sku, [])
        if not product_entries:
            missing.append(sku)
            continue

        for entry in product_entries:
            allocations = _allocate_stock(stock, entry["variants"])
            jobs.append({
                "base_sku": sku,
                "total_stock": stock,
                "product_id": entry["product_id"],
                "allocations": allocations,
            })

    matched_excel_skus = len(desired_stock) - len(missing)
    print(f"  Excel SKUs matched: {matched_excel_skus}")
    print(f"  Excel SKUs missing: {len(missing)}")
    print(f"  Total update calls planned: {len(jobs)}  (1 call per product)")
    if missing:
        print("  Missing SKUs (no base or variants found in catalog):")
        for sku in missing:
            print(f"    - {sku}")

    # Show fan-out so the operator notices when a base SKU touches many products.
    fanned_out = [
        (sku, len(base_to_products[sku]))
        for sku in desired_stock
        if len(base_to_products.get(sku, [])) > 1
    ]
    if fanned_out:
        print("  Base SKUs that exist on multiple products (each rebalanced independently):")
        for sku, count in fanned_out:
            print(f"    - {sku}: {count} products")
    print()

    if not jobs:
        print("Nothing to update. Exiting.")
        return

    # STEP 4: Push allocations to TikTok Shop, one PUT per product.
    print(f"[4/4] Issuing {len(jobs)} update call(s)...")
    success_count = 0
    failed = []
    for job in jobs:
        label = f"{job['base_sku']} (product {job['product_id']})"
        try:
            sku_updates = [
                (v["sku_id"], v["warehouse_id"], units)
                for v, units in job["allocations"]
            ]
            _update_stock_batch(job["product_id"], sku_updates)
            success_count += 1
            print(f"  ✓ {label}: {job['total_stock']} pcs")
            for variant, units in job["allocations"]:
                pcs = units * variant["multiplier"]
                print(
                    f"      {variant['seller_sku']}: "
                    f"{units} units × {variant['multiplier']}pc = {pcs} pcs"
                )
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
    Reads the Excel file and returns (desired_stock, skipped_variants).
      desired_stock:    dict of base_sku -> stock (int)
      skipped_variants: list of variant-pattern SKUs that were filtered out

    Variant-pattern SKUs (matching `<N>PCS-<...>`) are skipped with a
    warning so the operator can fix the Excel file. Base SKUs drive the
    rebalance across all variants automatically.
    """

    workbook = openpyxl.load_workbook(path, data_only=True)
    sheet = workbook.active

    result = {}
    skipped_variants = []
    for row_num, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if not row or len(row) < 2:
            continue

        sku_raw = row[0]
        stock_raw = row[1]

        if sku_raw is None or str(sku_raw).strip() == "":
            continue

        sku = str(sku_raw).strip()

        # Reject variant-pattern SKUs early: the operator should provide
        # the BASE SKU and let this script fan out to variants.
        if _VARIANT_PATTERN.match(sku):
            print(
                f"  Row {row_num}: SKU '{sku}' looks like a pack-size variant; "
                f"skipping (provide the base SKU instead)"
            )
            skipped_variants.append(sku)
            continue

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

    return result, skipped_variants


# ============================================================
# TikTok Shop product catalog fetching (NEW API 202502 search)
# ============================================================

def _parse_sku(seller_sku):
    """
    Returns (base_sku, multiplier) for a seller SKU string.

    Recognised variant pattern: "<digits>PCS-<base_sku>", case-sensitive.
    Anything else is its own base with multiplier 1. Multiplier 0 is
    treated as "not a variant" to avoid divide-by-zero downstream.
    """
    m = _VARIANT_PATTERN.match(seller_sku)
    if m:
        multiplier = int(m.group(1))
        if multiplier > 0:
            return m.group(2), multiplier
    return seller_sku, 1


def _build_base_sku_mapping():
    """
    Returns dict mapping base_sku -> list of:
        {
          "product_id": str,
          "variants": [
            {"multiplier": int, "sku_id": str, "warehouse_id": str, "seller_sku": str},
            ... sorted ascending by multiplier
          ]
        }

    Variants are grouped per (base_sku, product_id) so each PUT call
    targets a single product (TikTok's update endpoint cannot mix SKUs
    from multiple products).
    """

    # First pass: build product_id -> {base_sku -> [variant dicts]}.
    catalog = {}
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

        # Body intentionally empty — matches the working curl. Adding a
        # status filter here is supported but unnecessary; we want
        # everything in the catalog so the warehouse can update any SKU.
        response = tiktokshop_client._call_signed(
            "POST",
            "/product/202502/products/search",
            extra_query=extra_query,
            body={"status": "ACTIVATE"},
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

                base_sku, multiplier = _parse_sku(seller_sku)

                product_groups = catalog.setdefault(product_id, {})
                product_groups.setdefault(base_sku, []).append({
                    "multiplier": multiplier,
                    "sku_id": sku_id,
                    "warehouse_id": warehouse_id,
                    "seller_sku": seller_sku,
                })

        page_token = payload.get("next_page_token") or ""
        print(f"  Page {page_num}: {len(products)} products (running total: {products_seen})")
        if not page_token:
            break

        time.sleep(0.3)  # Be polite between pages.

    # Second pass: invert to base_sku -> [{product_id, variants}].
    # Sort variants ascending by multiplier so allocation can put any
    # remainder onto the smallest pack size by indexing [0].
    base_to_products = {}
    for product_id, groups in catalog.items():
        for base_sku, variants in groups.items():
            variants.sort(key=lambda v: v["multiplier"])
            base_to_products.setdefault(base_sku, []).append({
                "product_id": product_id,
                "variants": variants,
            })

    return base_to_products


# ============================================================
# Stock allocation across pack-size variants
# ============================================================

def _allocate_stock(total_pieces, variants):
    """
    Distributes total_pieces across pack-size variants.

    Returns a list of (variant_dict, units_to_set) tuples in the same
    order as the input variants (which must be sorted ascending by
    multiplier).

    Algorithm:
      share     = total_pieces // N         # pieces per variant
      units     = share // multiplier       # whole units per pack size
      remainder = total_pieces - sum(units * multiplier)
      Smallest variant absorbs (remainder // smallest.multiplier) extra units.

    A variant whose multiplier exceeds the share simply gets 0 units;
    those unfilled pieces flow to the smallest pack size as remainder.
    """
    n = len(variants)
    if n == 0:
        return []

    share = total_pieces // n

    allocations = []
    represented = 0
    for variant in variants:
        units = share // variant["multiplier"]
        allocations.append([variant, units])
        represented += units * variant["multiplier"]

    # Push leftover pieces to the smallest pack size (variants[0] after sort).
    remainder = total_pieces - represented
    if remainder > 0:
        smallest = allocations[0][0]
        extra_units = remainder // smallest["multiplier"]
        allocations[0][1] += extra_units

    return [(v, u) for v, u in allocations]


# ============================================================
# Inventory update (NEW API 202309), batched per product
# ============================================================

def _update_stock_batch(product_id, sku_updates):
    """
    POST /product/202309/products/{product_id}/inventory/update.

    Per-product endpoint: you can update multiple SKUs of the SAME
    product in one call, which is exactly what pack-size rebalancing
    needs (all variants of one base SKU live on one product).

    Args:
      product_id:  TikTok product id (string).
      sku_updates: list of (sku_id, warehouse_id, quantity) tuples.

    Raises:
      RuntimeError if TikTok returns an error or per-SKU failure.
    """

    path = f"/product/{_INVENTORY_API_VERSION}/products/{product_id}/inventory/update"
    body = {
        "skus": [
            {
                "id": sku_id,
                "inventory": [{"warehouse_id": warehouse_id, "quantity": quantity}],
            }
            for sku_id, warehouse_id, quantity in sku_updates
        ],
    }

    response = tiktokshop_client._call_signed("POST", path, body=body)
    tiktokshop_client._check_ok(response, context=f"update inventory product={product_id}")

    # Even with code=0, TikTok may report per-SKU failures inside data.
    # Field name has shifted across spec revisions; check both.
    data = response.json().get("data") or {}
    failures = data.get("errors") or data.get("failed_skus") or []
    if failures:
        raise RuntimeError(f"per-sku failures: {failures}")


if __name__ == "__main__":
    main()