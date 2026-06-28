# itbisa-tiktokshop-order-bot — ChatGPT Chat guide

Condensed `CLAUDE.md` for ChatGPT Chat (≤ 8000 chars); `CLAUDE.md` is the full source of truth. Always write "TikTok Shop" — never "TikTok".

## What it is
Python bot: fetch TikTok Shop orders → ship packages → download/send waybill labels to Telegram → dispatch stock balance once. Runs once per invocation, then exits. GitHub Actions only: no server, DB, or long-running process. **Track unit: `package_id`** (NOT `order_id`) — one order can have many packages, each with its own waybill and Telegram send.

## Stack & files (Python 3.11)
`src/main.py` (orchestration), `tiktokshop_client.py`, `tiktokshop_auth.py`, `label_processor.py`, `telegram_sender.py`, `state_manager.py`, `balance_dispatcher.py`, `balance_throttle.py`. Workflows: `run.yml` (execution), `ci.yml` (pytest on PRs). Tests in `tests/` (pure logic only).

## Constants & URLs
`TOKEN_REFRESH_BUFFER_MINUTES = 10`, `STATE_RETENTION_DAYS = 3`, `MAX_ORDERS_PER_RUN = 30`, `LABEL_IMAGE_DPI = 200`. `AUTH_BASE_URL = https://auth.tiktok-shops.com`, `OPEN_API_BASE_URL = https://open-api.tiktokglobalshop.com`. Document type: `SHIPPING_LABEL_AND_PACKING_SLIP`.

## State / tokens (committed to bot-state)
- `data/processed_orders.json`, `data/tiktokshop_tokens.json`, `data/balance_throttle.json`. Token fields: `access_token`, `refresh_token`, `access_token_expires_at`, `refresh_token_expires_at`. **Respect `refresh_token_expires_at`.** Save rotated tokens immediately after refresh.
- `main` = source; `bot-state` = runtime state/token files only — never protect it, never commit live tokens to `main`.

## Order flow (key invariants)
- Statuses `AWAITING_SHIPMENT`, `AWAITING_COLLECTION`. Extract `package_id` jobs from order packages, pairing each with its source order (caption). Drop already-processed `package_id`s.
- No new packages → save pruned state + heartbeat, no balance dispatch. New packages > `MAX_ORDERS_PER_RUN` → stop and alert.
- Batch-ship every package whose source order is `AWAITING_SHIPMENT`; `AWAITING_COLLECTION` packages are already shipped — only download the waybill.
- Per `package_id`: request shipping doc → `doc_url` → download PDF (no auth; pre-signed) → PNG, merge every 2 pages into 1 image → send → mark processed ONLY after Telegram confirms delivery → save state immediately → record each `seller_sku` from `order.line_items`.
- After the loop + final save: dispatch `/stock_balance` once with all touched base SKUs in a single `workflow_dispatch`. Heartbeat includes the balance result.

## Label flow
GET `/fulfillment/202309/packages/{package_id}/shipping_documents`, `document_type = SHIPPING_LABEL_AND_PACKING_SLIP` → `doc_url`. **Download `doc_url` without TikTok Shop auth** (pre-signed). Retry within the run if `doc_url`/PDF not ready; else skip and retry next run.

## Auth
Plain unsigned GET. Refresh: `https://auth.tiktok-shops.com/api/v2/token/refresh`.

## Open API signing
- Signed; include `x-tts-access-token` header. Query params: `app_key`, `shop_id`, `timestamp`, `version`, usually `shop_cipher`.
- Exclude `sign`, `access_token`, and empty values from the signature. Sort params by key, concatenate `key + value`. Canonical = `path + sorted params + raw body string`, wrapped with `app_secret` at both ends. HMAC-SHA256 with `app_secret`, hex lowercase. Body signed + sent byte-for-byte identically.
- `shop_cipher` required for most calls; fetch from `/authorization/202309/shops` (`include_cipher=false`). Cache once per run.

## API quirks (do not regress)
- The `orders` key is **omitted entirely** (not an empty list) when there are no results — use `.get()` with empty defaults.
- All endpoints used here are version `202309` (orders search `/order/202309/orders/search`, ship, shipping documents, shops). No `202502` search family.

## Telegram output
- Bahasa Indonesia. Caption lines: `• {qty} x {sku}` — single space, no indent; multi-courier orders inline per SKU: `• {qty} x {sku} ({courier})`. Sent `parse_mode=Markdown`; order number, courier, SKU wrapped in backtick code spans (`_mono`, strips backticks) so they are tap-to-copy.
- Heartbeat uses plain label `TikTok Shop` (hardcoded in `build_summary`; no `TIKTOKSHOP_LABEL` constant): e.g. `✅ TikTok Shop - 12:00 - 3 label terkirim`, `⚠️ TikTok Shop - 13:00 - 2 terkirim, 1 gagal (akan dicoba lagi)`. Append `⚖️ Stock Balance: X/Y SKU dipicu` when balance fired, or `⏳ Stock Balance: N SKU menunggu (maks. 1× / N jam)` when throttle-deferred (`_format_balance_line`). Use "stock" not "inventory" (except real endpoints like `/inventory/update`).

## balance_dispatcher.py — duplicated across both order bots intentionally
- `class BalanceDispatcher`: `record(sku)`, `collected()`, `dispatch_all()`. `record()`/`to_base_sku()`: strips leading `^\d+PCS-`, uppercases, ignores empty/None, dedupes via internal set.
- `dispatch_all()`: ONE `workflow_dispatch` on `furqonajiy/itbisa-shop-stock-bot/balance.yml`, `ref=main`, `sku` = collected base SKUs space-joined, `dry_run=false`. One HTTP call regardless of count.
- Needs env `STOCK_DISPATCH_TOKEN`; if missing, all SKUs reported failed and run still finishes. Returns `{requested, dispatched, failed, skus}`. Best-effort: failure logged + in heartbeat, **never raised**.
- Records via `order["line_items"][].seller_sku` (already the variant SKU). Over-recording is harmless — deduped + `/stock_balance` idempotent. `record()` only in the success branch; dispatch once after the loop + final save. Do not factor out the duplicate.
- **Throttle (`balance_throttle.py`, duplicated):** `MIN_INTERVAL_HOURS` (currently `1`) = min spacing between dispatches; bursts in the same hour collapse to one dispatch (saves Actions minutes; `0` = every run). SKUs withheld accumulate in `pending_skus` (`data/balance_throttle.json` on `bot-state`) and flush in one dispatch when the window reopens, so none are dropped (`/stock_balance` idempotent). `_run_throttled_balance` (`main.py`): load → `merge_pending` → if `window_open` flush all pending (reset window on success) else defer.

## Workflow (run.yml)
`workflow_dispatch` only (manual or dispatched by the Telegram Worker); no schedule/cron. Checkout `main` as source; overlay `data/` from `bot-state`; run once; commit updated state/token files to `bot-state` with `if: always()`. Concurrency group `bot-state-${{ github.repository }}`, `cancel-in-progress: false`, `timeout-minutes: 10`. Idle-run efficiency: an `id: precheck` step runs `python -m src.main --precheck` (no poppler) and emits `has_work=false` only on a clean zero-new-packages result (sending the heartbeat + saving state itself); the poppler install + full run are gated `if: steps.precheck.outputs.has_work != 'false'` (fail-safe). `poppler-utils` (pdf2image), Python 3.11 pip-cached, `actions/checkout@v5+`, `actions/setup-python@v6+`, `permissions.contents: write`. Run-step env needs `STOCK_DISPATCH_TOKEN`.

## Secrets
TikTok Shop `app_key`/`secret`/`shop_id`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `STOCK_DISPATCH_TOKEN`. Never hardcode.

## Workflow & identity (process standard)
- Commits/PRs authored as `C - Furqon Aji Yudhistira <furqonajiy@gmail.com>`. **No AI references** anywhere — no Co-Authored-By, no "Generated by", no session links.
- Branch `feature/<desc>` off `main`; PR into `main`; merge commit (`--no-ff`); merge title ends with `(#PR)`. Docs + marker ride in the same PR. Maintainer on Windows — CLI commands in PowerShell.

## Flag before changing
State/token format (incl. `refresh_token_expires_at`), Open API signing / canonical string / `shop_cipher`, `bot-state`, `workflow_dispatch`-only trigger, `package_id` track unit, `seller_sku` recording, `balance_dispatcher`/`balance_throttle` batching + best-effort model, label flow (`doc_url` no-auth download), `202309` endpoint usage, workflow concurrency (`cancel-in-progress: false`), Telegram chat authorization, token rotation.
