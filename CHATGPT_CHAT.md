# itbisa-tiktokshop-order-bot — ChatGPT guide

Condensed `CLAUDE.md` (≤8000 chars); `CLAUDE.md` is the source of truth. Always write "TikTok Shop" — never "TikTok".

## What it is
Python bot: TikTok Shop orders → ship packages → send waybill labels to Telegram → dispatch stock balance once. One run per invocation. GitHub Actions only: no server, DB, long-running process. **Track unit: `package_id`** (NOT `order_id`) — one order can have many packages, each with its own waybill and Telegram send.

## Stack & files (Python 3.11)
`src/main.py`, `tiktokshop_client.py`, `tiktokshop_auth.py`, `label_processor.py`, `telegram_sender.py`, `state_manager.py`, `balance_dispatcher.py`, `balance_throttle.py`. Workflows: `run.yml`, `ci.yml` (pytest on PRs). Tests: `pytest -q`; extend `tests/`, never add a runner.

## Constants & URLs
`TOKEN_REFRESH_BUFFER_MINUTES = 10`, `STATE_RETENTION_DAYS = 3`, `MAX_ORDERS_PER_RUN = 30`, `LABEL_IMAGE_DPI = 200`. `TIKTOKSHOP_AUTH_BASE_URL = https://auth.tiktok-shops.com`, `TIKTOKSHOP_OPEN_API_BASE_URL = https://open-api.tiktokglobalshop.com`. Doc: `SHIPPING_LABEL_AND_PACKING_SLIP`.

## State / tokens (committed to bot-state)
- `data/processed_orders.json`, `data/tiktokshop_tokens.json`, `data/balance_throttle.json`. Token fields: `access_token`, `refresh_token`, `access_token_expires_at`, `refresh_token_expires_at` — **respect it**. Save rotated tokens at once.
- `main` = source; `bot-state` = runtime state/token files only — never protect it, never commit live tokens to `main`.

## Order flow (key invariants)
- Statuses `AWAITING_SHIPMENT`, `AWAITING_COLLECTION`. Extract `package_id` jobs from order packages, pairing each with its source order (caption). Drop already-processed `package_id`s.
- No new packages → save pruned state + heartbeat, no balance dispatch. New packages > `MAX_ORDERS_PER_RUN` → stop and alert.
- Batch-ship every package whose source order is `AWAITING_SHIPMENT`; `AWAITING_COLLECTION` packages are already shipped — only download the waybill.
- Per `package_id`: shipping doc → `doc_url` → download PDF (no auth) → PNG, merge every 2 pages → send → mark processed ONLY after Telegram confirms → save state → record each `seller_sku`.
- After the loop + final save: dispatch `/stok_balance` (legacy alias: `/stock_balance`) once with all touched base SKUs in a single `workflow_dispatch`. Heartbeat includes the balance result.

## Label flow
GET `/fulfillment/202309/packages/{package_id}/shipping_documents`, `document_type = SHIPPING_LABEL_AND_PACKING_SLIP` → `doc_url`. **Download `doc_url` without auth** (pre-signed). `get_waybill_pdf` returns `None` **only** while still generating (retried in-run, then skip + retry next run); **every other non-zero code RAISES** with code/message/request_id → `resi gagal dibuat`. Envelope validated before `code` is read; hard failures not retried. Pending matched on the message — codes undocumented, never invent one.

## Auth
Plain unsigned GET. Refresh: `https://auth.tiktok-shops.com/api/v2/token/refresh`.

## Open API signing
- Signed; include `x-tts-access-token` header. Query params: `app_key`, `shop_id`, `timestamp`, `version`, usually `shop_cipher`.
- Exclude `sign`, `access_token`, empty values from the signature. Sort params by key, concatenate `key + value`. Canonical = `path + sorted params + raw body`, wrapped with `app_secret` both ends; HMAC-SHA256, hex lowercase. Body signed + sent byte-for-byte identically.
- `shop_cipher` required for most calls; fetch from `/authorization/202309/shops` (`include_cipher=false`). Cache once per run.

## API quirks (do not regress)
- The `orders` key is **omitted entirely** (not an empty list) when there are no results — use `.get()` with defaults.
- All endpoints used here are version `202309` (orders search `/order/202309/orders/search`, ship, shipping documents, shops). No `202502` search family.

## Telegram output
- Bahasa Indonesia. Caption lines: `• {qty} x {sku}` — single space, no indent; multi-courier inline: `• {qty} x {sku} ({courier})`. `parse_mode=Markdown`; order number, courier, SKU in backtick code spans (`_mono`, strips backticks) → tap-to-copy.
- Heartbeat label `TikTok Shop` (hardcoded in `build_summary`): `⚠️ … 2 terkirim, 1 menunggu TikTok Shop, 1 gagal` + `⏳/❌ {package_id} — {reason}` (cap 10/group). Appends `⚖️ Stock Balance: X/Y SKU dipicu` / `⏳ … N SKU menunggu` (`_format_balance_line`). Use "stock" not "inventory".

## balance_dispatcher.py — duplicated across both order bots intentionally
- `class BalanceDispatcher`: `record(sku)`, `collected()`, `dispatch_all()`. `record()`/`to_base_sku()`: strips leading `^\d+PCS-`, uppercases, ignores empty/None, dedupes via internal set.
- `dispatch_all()`: ONE `workflow_dispatch` on `furqonajiy/itbisa-shop-stock-bot/balance.yml`, `ref=main`, `sku` = collected base SKUs space-joined, `dry_run=false`.
- Needs env `STOCK_DISPATCH_TOKEN`; if missing, all SKUs reported failed and run still finishes. Returns `{requested, dispatched, failed, skus}`. Best-effort: failure logged + in heartbeat, **never raised**.
- Records via `order["line_items"][].seller_sku` (already the variant SKU). Over-recording is harmless — deduped + `/stok_balance` idempotent. `record()` only in the success branch. Do not factor out the duplicate.
- **Throttle (`balance_throttle.py`, duplicated):** `MIN_INTERVAL_HOURS` (currently `1`) = min dispatch spacing; bursts collapse to one (`0` = every run). Withheld SKUs accumulate in `pending_skus` (`data/balance_throttle.json`) and flush together when the window reopens, so none are dropped. `_run_throttled_balance`: load → `merge_pending` → if `window_open` flush all (reset window on success) else defer. **Every run drains the queue, including a no-new-packages run** — the early return used to skip the flush, stranding a SKU until an unrelated package arrived. Idle path uses the same flush with the empty collector; heartbeat line only when `requested > 0`.

## Workflow (run.yml)
`workflow_dispatch` only; no cron. Checkout `main`; overlay `data/` from `bot-state`; run once; commit state/token files back with `if: always()`. Concurrency `bot-state-${{ github.repository }}`, `cancel-in-progress: false`; `timeout-minutes: 10`. Idle: `id: precheck` runs `--precheck` (no poppler), emits `has_work=false` only on a clean zero-new-packages result (sends the heartbeat, saves state + drains the pending balance queue itself); poppler + full run gated on `has_work != 'false'` (fail-safe). Python 3.11 pip-cached, `checkout@v5+`, `setup-python@v6+`, `contents: write`. Run-step **and precheck-step** env need `STOCK_DISPATCH_TOKEN`.

## Secrets
TikTok Shop `app_key`/`secret`/`shop_id`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `STOCK_DISPATCH_TOKEN`. Never hardcode.

## Workflow & identity (process standard)
- Commits/PRs authored as `C - Furqon Aji Yudhistira <furqonajiy@gmail.com>`. **No AI references** anywhere — no Co-Authored-By, no "Generated by", no session links.
- Branch `feature/<desc>` off `main`; PR into `main`; merge commit (`--no-ff`); merge title ends with `(#PR)`. Docs + marker ride in the same PR. Maintainer on Windows — CLI commands in PowerShell.

## Flag before changing
The shipping-document pending-vs-hard split (`_is_document_pending`; `None` = still generating only, all else raises, envelope validated, no retry on hard errors, message-keyed), the idle-run balance drain (no-new-packages path still flushes; heartbeat line only when `requested > 0`; `STOCK_DISPATCH_TOKEN` in the precheck env), state/token format (incl. `refresh_token_expires_at`), Open API signing / canonical string / `shop_cipher`, `bot-state`, `workflow_dispatch`-only trigger, `package_id` track unit, `seller_sku` recording, `balance_dispatcher`/`balance_throttle` batching + best-effort model, label flow, `202309` endpoint usage, workflow concurrency, Telegram chat authorization, token rotation.
