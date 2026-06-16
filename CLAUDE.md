# CLAUDE.md — itbisa-tiktokshop-order-bot

> **Single source of truth for this repo.** Read automatically by Claude Code and pasted into the Claude Chat project. `AGENTS.md` (ChatGPT Codex) points here; `CHATGPT_CHAT.md` is the ≤ 8000-char condensed copy for ChatGPT Chat. Keep all three at the repo root.

Python bot: fetch TikTok Shop orders → ship packages → download/send waybill labels to Telegram → dispatch stock balance once. Runs once per invocation, then exits.

Always write "TikTok Shop" / `tiktokshop` / `TIKTOKSHOP`. **Never shorten to "TikTok"** when referring to shop APIs or bot behavior.

## Stack & files
- Python 3.11.
- `src/main.py` (orchestration), `src/tiktokshop_client.py`, `src/tiktokshop_auth.py`, `src/label_processor.py`, `src/telegram_sender.py`, `src/state_manager.py`, `src/balance_dispatcher.py`.
- Workflow: `.github/workflows/run.yml` (execution, `workflow_dispatch`); `ci.yml` (quality gate — runs `pytest` on PRs/pushes; no secrets, never touches `bot-state`).
- Tests: `tests/` (pytest). Pure logic only — `balance_dispatcher` (`to_base_sku`, dedup, best-effort no-token dispatch) and `telegram_sender` caption helpers (`_mono`, `build_caption` incl. multi-courier inline). Dev deps in `requirements-dev.txt`; run `pytest -q`. Network/API and the label flow are not unit-tested.
- **Track unit: `package_id`** (NOT `order_id`). One order can have multiple packages; each package has its own waybill and its own Telegram send.

## Constants & URLs
`TOKEN_REFRESH_BUFFER_MINUTES = 10`, `STATE_RETENTION_DAYS = 3`, `MAX_ORDERS_PER_RUN = 30`, `LABEL_IMAGE_DPI = 200`.
`TIKTOKSHOP_AUTH_BASE_URL = https://auth.tiktok-shops.com`. `TIKTOKSHOP_OPEN_API_BASE_URL = https://open-api.tiktokglobalshop.com`.
Document type: `SHIPPING_LABEL_AND_PACKING_SLIP`.

## State / tokens (committed to bot-state)
- `data/processed_orders.json`, `data/tiktokshop_tokens.json`.
- Token file fields: `access_token`, `refresh_token`, `access_token_expires_at`, `refresh_token_expires_at`. **Respect `refresh_token_expires_at`.**
- Save rotated tokens immediately after refresh.

## Order flow (key invariants)
- Statuses: `AWAITING_SHIPMENT`, `AWAITING_COLLECTION`.
- Extract `package_id` jobs from order packages, pairing each with its source order (for the caption). Drop already-processed `package_id`s.
- No new packages → save pruned state + heartbeat, no balance dispatch.
- New packages > `MAX_ORDERS_PER_RUN` → stop and alert via Telegram.
- Batch-ship every package whose source order is `AWAITING_SHIPMENT`. `AWAITING_COLLECTION` packages are already shipped — only download the waybill.
- Per `package_id`: request shipping document → get `doc_url` → download PDF (no auth; pre-signed) → convert to PNG, merge every 2 pages into 1 image → send → mark processed ONLY after Telegram confirms delivery → save state immediately → record every `seller_sku` from `order.line_items` into the balance dispatcher.
- After the loop + final save: dispatch `/stock_balance` once with all touched base SKUs in a single `workflow_dispatch`.
- Heartbeat summary includes the balance result.

## Label flow
GET `/fulfillment/202309/packages/{package_id}/shipping_documents`, `document_type = SHIPPING_LABEL_AND_PACKING_SLIP`. Response has `doc_url`. **Download `doc_url` without TikTok Shop auth** (pre-signed). Retry within the run if `doc_url`/PDF not ready; if still not ready, skip and retry next run.

## Auth
- Auth calls are plain unsigned GET. Refresh endpoint: `https://auth.tiktok-shops.com/api/v2/token/refresh`.

## Open API signing
- Open API calls must be signed; include `x-tts-access-token` header.
- Query params include `app_key`, `shop_id`, `timestamp`, `version`, and usually `shop_cipher`.
- Exclude `sign` and `access_token` from signature. Exclude empty values. Sort params by key. Concatenate `key + value`.
- Canonical string = `path + sorted params + raw body string`. Wrap canonical with `app_secret` at both ends. HMAC-SHA256 with `app_secret`. Hex lowercase output.
- Body must be signed and sent byte-for-byte identically.
- `shop_cipher` required for most calls. Fetch from `/authorization/202309/shops` with `include_cipher=false`. Cache `shop_cipher` once per process run.

## API quirks (do not regress)
- The `orders` key is **omitted entirely** (not an empty list) when there are no results — use `.get()` with empty defaults.
- All Open API endpoints used here are version `202309` (orders search `/order/202309/orders/search`, ship, shipping documents, shops). This order bot does not use the `202502` search family.

## Telegram output
- Bahasa Indonesia.
- Caption item lines: `• {qty} x {sku}` — single space, no leading indent. For orders with multiple distinct couriers, inline per SKU: `• {qty} x {sku} ({courier})`. The caption is sent with `parse_mode=Markdown`; order number, courier, and SKU are wrapped in backtick code spans (`_mono`) so they are tap-to-copy. `_mono` strips backticks from the value so a code span can never break and fail the label send.
- Heartbeat uses the plain label `TikTok Shop` (hardcoded in `telegram_sender.build_summary`; no `TIKTOKSHOP_LABEL` constant in this repo):
    - `✅ TikTok Shop - 11:00 - Tidak ada pesanan baru`
    - `✅ TikTok Shop - 12:00 - 3 label terkirim`
    - `⚠️ TikTok Shop - 13:00 - 2 terkirim, 1 gagal (akan dicoba lagi)`
- Append `⚖️ Stock Balance: X/Y SKU dipicu` when balance fired this run.

## balance_dispatcher.py — duplicated across both order bots intentionally
- `class BalanceDispatcher` with `record(sku)` and `dispatch_all()`.
- `record()`/`to_base_sku()`: strips leading `^\d+PCS-` and uppercases; ignores empty/None; dedupes via internal set.
- `dispatch_all()`: fires a SINGLE `workflow_dispatch` on `furqonajiy/itbisa-shop-stock-bot/balance.yml`, `ref=main`, `sku` = all collected base SKUs space-joined, `dry_run=false`. One HTTP call regardless of count.
- Requires env `STOCK_DISPATCH_TOKEN`. If missing, all collected SKUs reported failed; the run still finishes normally.
- Returns `{requested, dispatched, failed, skus}`; counts reflect SKUs.
- Best-effort: failure is logged and reported in the heartbeat, **never raised**.
- TikTok Shop records via `order["line_items"][].seller_sku` (already the variant SKU). Over-recording is harmless — the dispatcher dedupes and `/stock_balance` is idempotent. `record()` is called only in the success branch; `dispatch_all()` once after the loop + final save.

## Workflow (run.yml) — required config
- Trigger: `workflow_dispatch` only (manual from the Actions tab, or dispatched by the Telegram Worker). No `schedule`/cron.
- Checkout `main` as source; overlay `data/` from `bot-state`; run the bot once; commit updated state/token files to `bot-state` with `if: always()`.
- Concurrency: group `bot-state-${{ github.repository }}`, `cancel-in-progress: false`.
- Install `poppler-utils` (pdf2image). Python 3.11. `actions/checkout@v5+`, `actions/setup-python@v6+`. `permissions.contents: write`.
- Run-step env must include `STOCK_DISPATCH_TOKEN`.

## Secrets
TikTok Shop `app_key`/`secret`/`shop_id`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `STOCK_DISPATCH_TOKEN`.

## Global architecture & conventions (shared across all ITBisa repos)
- GitHub Actions only. No VM, server, database, queue, or long-running process.
- `main` = source code. `bot-state` = runtime state/token files only. Never protect `bot-state`. Never commit live token files to `main`.
- Never hardcode secrets.
- Self-contained repo, no shared library — `balance_dispatcher.py` is duplicated across the order bots on purpose; do not factor it out.
- Minimal, targeted changes only. No broad refactors; preserve existing behavior unless explicitly in scope.
- Telegram user-facing strings: Bahasa Indonesia. Use "stock", not "inventory" (except real endpoint names such as `/inventory/update`).
- Platform label: heartbeats use the plain string `TikTok Shop` (no glyph, no label constant in this repo). Single-space formatting; no multi-space alignment.
- Runtime dispatch/checkout ref is `main`. `feature/improve` must be merged to `main` before production uses it.

## Development workflow (process standard)
- Branch from `main` using `feature/<short-description>` (e.g. `feature/document-dev-workflow`).
- Always open a PR into `main` and **merge with a merge commit (`--no-ff`)** — never squash, never fast-forward — so the feature branch stays an ancestor of `main`.
- The merge commit title must be **representative and end with the PR number**, e.g. `Update Project Instructions to the Latest State (#47)` — never the default `Merge pull request …`.
- Commits and PRs are authored as **`C - Furqon Aji Yudhistira <furqonajiy@gmail.com>`** — never an AI/assistant identity.
- **No AI/assistant references anywhere** — not in branch names, commit messages, PR titles/bodies, code, or comments. No mention of any AI assistant by name, no `Co-Authored-By` trailer, no "Generated with/by" line, no model ids, no session links. History must read as human-authored. (If the PR tool auto-appends such a footer, edit the PR body to remove it and re-check.)
- CLI commands handed to the maintainer are given in **PowerShell** syntax (Windows).
- Keep changes minimal and targeted; update `CLAUDE.md` / `README.md` in the same PR whenever behavior or process changes.
- **AI-instruction files (repo root, auto-discovered):** `CLAUDE.md` is the single source of truth — read by Claude Code and pasted into the Claude Chat project (no tight size cap). `AGENTS.md` is a thin pointer to `CLAUDE.md` for ChatGPT Codex, carrying the author-identity / no-AI-refs / feature→PR→merge / PowerShell rules inline. `CHATGPT_CHAT.md` is a ≤ 8000-char condensed copy of this file for ChatGPT Chat (its project-instruction limit). Update these **only when explicitly asked**, and keep `CHATGPT_CHAT.md` in step with `CLAUDE.md`.
- Sync marker: a file named `YYYY-MM-DD_HHMM.txt` (WIB) sits at the repo root. **On every update to this repo, rename it to the current WIB timestamp** — it signals whether the repo and the AI-instruction files are in sync.
- Doc/marker updates (`CLAUDE.md`, `AGENTS.md`, `CHATGPT_CHAT.md`, the sync marker) ride in the **same feature branch and PR as the related code change** — never a separate doc-only branch (avoids noise).

## Flag before changing
State/token format (incl. `refresh_token_expires_at`), Open API signing / canonical string / `shop_cipher`, `bot-state`, `workflow_dispatch`-only trigger, the `package_id` track unit, `seller_sku` recording, `balance_dispatcher` batching / best-effort model, label flow (`doc_url` no-auth download), `202309` endpoint usage, workflow concurrency (`cancel-in-progress: false`), Telegram chat authorization, token rotation.