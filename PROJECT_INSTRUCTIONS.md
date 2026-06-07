# Project Instructions — itbisa-tiktokshop-order-bot

> Synced source for **Claude** and **ChatGPT** project instructions — paste this same text into both. Keep ≤ 8000 characters (ChatGPT limit, incl. spaces). Update only when explicitly requested.

Always write "TikTok Shop" / `tiktokshop` / `TIKTOKSHOP`. **Never shorten to "TikTok".**

## What this is
Python bot: fetch TikTok Shop orders → ship packages → download/send waybill labels to Telegram → dispatch stock balance once. Runs once per invocation, then exits. **Track unit: `package_id`** (NOT `order_id`). One order can have multiple packages; each has its own waybill and Telegram send.

## Stack & files
- Python 3.11. `src/main.py` (orchestration), `src/tiktokshop_client.py`, `src/tiktokshop_auth.py`, `src/label_processor.py`, `src/telegram_sender.py`, `src/state_manager.py`, `src/balance_dispatcher.py`. Workflow: `.github/workflows/run.yml`.

## Constants & URLs
`TOKEN_REFRESH_BUFFER_MINUTES = 10`, `STATE_RETENTION_DAYS = 3`, `MAX_ORDERS_PER_RUN = 30`, `LABEL_IMAGE_DPI = 200`. `TIKTOKSHOP_AUTH_BASE_URL = https://auth.tiktok-shops.com`. `TIKTOKSHOP_OPEN_API_BASE_URL = https://open-api.tiktokglobalshop.com`. Document type: `SHIPPING_LABEL_AND_PACKING_SLIP`.

## State / tokens (committed to bot-state)
- `data/processed_orders.json`, `data/tiktokshop_tokens.json`.
- Token fields: `access_token`, `refresh_token`, `access_token_expires_at`, `refresh_token_expires_at`. **Respect `refresh_token_expires_at`.** Save rotated tokens immediately.

## Order flow (key invariants)
- Statuses: `AWAITING_SHIPMENT`, `AWAITING_COLLECTION`.
- Extract `package_id` jobs from order packages, pairing each with its source order (for the caption). Drop already-processed `package_id`s.
- No new packages → save pruned state + heartbeat, no balance dispatch. New packages > `MAX_ORDERS_PER_RUN` → stop and alert via Telegram.
- Batch-ship every package whose source order is `AWAITING_SHIPMENT`. `AWAITING_COLLECTION` packages are already shipped — only download the waybill.
- Per `package_id`: request shipping document → get `doc_url` → download PDF (no auth; pre-signed) → convert to PNG, merge every 2 pages into 1 image → send → mark processed ONLY after Telegram confirms delivery → save state immediately → record every `seller_sku` from `order.line_items` into the balance dispatcher.
- After the loop + final save: dispatch `/stock_balance` once with all touched base SKUs in a single `workflow_dispatch`. Heartbeat includes the balance result.

## Label flow
GET `/fulfillment/202309/packages/{package_id}/shipping_documents`, `document_type = SHIPPING_LABEL_AND_PACKING_SLIP`. Response has `doc_url`. **Download `doc_url` without TikTok Shop auth** (pre-signed). Retry within the run if not ready; else skip and retry next run.

## Auth
Auth calls are plain unsigned GET. Refresh endpoint: `https://auth.tiktok-shops.com/api/v2/token/refresh`.

## Open API signing
- Signed; include `x-tts-access-token` header. Query params include `app_key`, `shop_id`, `timestamp`, `version`, usually `shop_cipher`.
- Exclude `sign` and `access_token` from signature. Exclude empty values. Sort params by key. Concatenate `key + value`.
- Canonical = `path + sorted params + raw body string`. Wrap with `app_secret` at both ends. HMAC-SHA256 with `app_secret`. Hex lowercase. Body signed and sent byte-for-byte identically.
- `shop_cipher` required for most calls. Fetch from `/authorization/202309/shops` with `include_cipher=false`. Cache once per process run.

## API quirks (do not regress)
- The `orders` key is **omitted entirely** (not an empty list) when there are no results — use `.get()` with empty defaults.
- All Open API endpoints used here are version `202309` (orders search `/order/202309/orders/search`, ship, shipping documents, shops). This order bot does **not** use the `202502` search family.

## Telegram output
- Bahasa Indonesia. Caption item lines: `• {qty} x {sku}` — single space, no leading indent. For multiple distinct couriers, inline per SKU: `• {qty} x {sku} ({courier})`.
- Heartbeat uses the plain label `TikTok Shop` (hardcoded; no `TIKTOKSHOP_LABEL` constant): `✅ TikTok Shop - 12:00 - 3 label terkirim`, `⚠️ TikTok Shop - 13:00 - 2 terkirim, 1 gagal (akan dicoba lagi)`.
- Append `⚖️ Stock Balance: X/Y SKU dipicu` when balance fired this run.

## balance_dispatcher.py (duplicated across both order bots intentionally)
- `BalanceDispatcher` with `record(sku)` and `dispatch_all()`. `record()`/`to_base_sku()`: strips leading `^\d+PCS-`, uppercases, dedupes.
- `dispatch_all()`: a SINGLE `workflow_dispatch` on `furqonajiy/itbisa-shop-stock-bot/balance.yml`, `ref=main`, `sku` space-joined, `dry_run=false`. Requires env `STOCK_DISPATCH_TOKEN`. Best-effort, **never raised**. Records via `order["line_items"][].seller_sku` (success branch only; over-recording harmless — dispatcher dedupes, `/stock_balance` idempotent).

## Workflow (run.yml)
- Trigger: `workflow_dispatch` only (manual or Telegram Worker). **No cron/schedule.**
- Checkout `main`; overlay `data/` from `bot-state`; run once; commit state/token files to `bot-state` with `if: always()`. Concurrency: group `bot-state-${{ github.repository }}`, `cancel-in-progress: false`. Install `poppler-utils`. Python 3.11. `actions/checkout@v5+`, `actions/setup-python@v6+`. `permissions.contents: write`. Run-step env must include `STOCK_DISPATCH_TOKEN`.

## Secrets
TikTok Shop `app_key`/`secret`/`shop_id`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `STOCK_DISPATCH_TOKEN`.

## Conventions
GitHub Actions only. `main` = source; `bot-state` = runtime state/token files only (never protect; never commit live tokens to `main`). Never hardcode secrets. Self-contained repo; `balance_dispatcher.py` duplicated on purpose. Telegram strings Bahasa Indonesia; use "stock" not "inventory". Runtime ref is `main`.

## Development workflow (process standard)
- Branch from `main` using `feature/<short-description>`.
- Always open a PR into `main` and **merge with a merge commit (`--no-ff`)** — never squash, never fast-forward.
- Commits and PRs are authored as **`C - Furqon Aji Yudhistira <furqonajiy@gmail.com>`** (never "Claude").

## Flag before changing
State/token format (incl. `refresh_token_expires_at`), Open API signing / canonical string / `shop_cipher`, `bot-state`, `workflow_dispatch`-only trigger, the `package_id` track unit, `seller_sku` recording, `balance_dispatcher` batching / best-effort model, label flow (`doc_url` no-auth download), `202309` endpoint usage, workflow concurrency (`cancel-in-progress: false`), Telegram chat authorization, token rotation.
