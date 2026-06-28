# ITBisa TikTok Shop Order Bot

Automatically processes TikTok Shop packages that need shipping labels,
downloads the package shipping documents, converts them to Telegram-friendly
PNG images, and sends them to the warehouse Telegram chat so labels can be
printed from a phone without manual downloading.

Everything runs as a short-lived GitHub Actions job. There is no server, no
cloud VM, and no database.

## Real production flow

Each run (dispatched manually or by the Telegram Worker — `workflow_dispatch` only, no cron) does one processing cycle:

1. GitHub Actions checks out `main` for source code.
2. The workflow overlays `data/processed_orders.json`,
   `data/tiktokshop_tokens.json`, and `data/balance_throttle.json` from the
   `bot-state` branch when available.
3. The Python bot loads already processed `package_id` values.
4. TikTok Shop access tokens are refreshed if they are near expiry.
5. The bot fetches the authorized shop cipher from
   `/authorization/202309/shops` and caches it for the current run.
6. The bot searches TikTok Shop orders in these statuses:
    - `AWAITING_SHIPMENT`
    - `AWAITING_COLLECTION`
7. It extracts unique `package_id` values from the returned orders.
8. It skips packages already present in `processed_orders.json`.
9. If the number of new packages exceeds `MAX_ORDERS_PER_RUN`, it stops before
   API writes and alerts Telegram.
10. It batch-ships all new packages whose source order is still
    `AWAITING_SHIPMENT` by calling `/fulfillment/202309/packages/ship`.
11. Packages already in `AWAITING_COLLECTION` are not shipped again; the bot
    only downloads their waybill.
12. For each new package, it requests
    `/fulfillment/202309/packages/{package_id}/shipping_documents` with
    document type `SHIPPING_LABEL_AND_PACKING_SLIP`.
13. TikTok Shop returns a pre-signed `doc_url`; the PDF is downloaded directly
    from that URL without TikTok Shop signing.
14. The PDF is rendered to PNG images. Every 2 PDF pages are merged into 1
    Telegram image to reduce message count while keeping page order unchanged.
15. The label image(s) are sent to Telegram with a Bahasa Indonesia caption.
16. A `package_id` is marked processed only after Telegram confirms delivery.
    State is saved immediately after each successful package.
17. After the loop, it dispatches the stock bot's `/stock_balance` once with all
    touched base SKUs (single `workflow_dispatch`; best-effort, never fatal).
    The dispatch is throttled to at most one balance run per
    `balance_throttle.MIN_INTERVAL_HOURS` (currently 1 hour) to conserve
    GitHub Actions minutes; base SKUs touched while throttled accumulate in
    `data/balance_throttle.json` and flush together when the window reopens, so
    no touched SKU is ever dropped.
18. At the end, the bot sends a heartbeat summary to Telegram. The heartbeat
    appends `⚖️ Stock Balance: X/Y SKU dipicu` when a balance was dispatched, or
    `⏳ Stock Balance: N SKU menunggu (maks. 1× / N jam)` when the dispatch was
    deferred by the throttle.
19. The workflow commits updated runtime state and rotated tokens back to
    `bot-state`.

The tracking unit is `package_id`, not `order_id`, because one TikTok Shop
order can contain multiple packages and each package has its own label.

## Project structure

```text
itbisa-tiktokshop-order-bot/
├── .github/workflows/
│   ├── run.yml                      # GitHub Actions workflow_dispatch + bot-state sync
│   └── ci.yml                       # Quality gate: pytest on PRs (no secrets, no bot-state)
├── data/                            # Runtime state bootstrap files
│   ├── processed_orders.json        # package_id values already sent to Telegram
│   ├── tiktokshop_tokens.json       # access_token + refresh_token bundle
│   └── balance_throttle.json        # /stock_balance throttle state + pending SKUs
├── scripts/
│   ├── bootstrap_tokens.py          # One-time / recovery token bootstrap
│   ├── get_tiktokshop_chiper_code.py           # Diagnostic helper; name kept as-is
│   └── test_telegram.py             # Telegram send diagnostic
├── src/
│   ├── __init__.py
│   ├── main.py                      # Entry point, orchestrates one run
│   ├── config.py                    # Env vars, API hosts, paths, constants
│   ├── tiktokshop_auth.py           # Token lifecycle + refresh-token expiry guard
│   ├── tiktokshop_client.py         # TikTok Shop Open API calls + signing
│   ├── label_processor.py           # PDF → PNG conversion + 2-page merge
│   ├── telegram_sender.py           # Telegram labels + Bahasa status messages
│   ├── state_manager.py             # Loads/saves processed package state
│   ├── balance_dispatcher.py        # Dispatches /stock_balance once after the run
│   └── balance_throttle.py          # 1×/window dispatch throttle + pending-SKU queue
├── tests/                           # pytest unit tests (pure logic only)
├── requirements.txt
├── requirements-dev.txt             # Dev/test dependencies (pytest)
├── pytest.ini
├── conftest.py
├── .env.example
├── .gitignore
└── README.md
```

## Requirements

- Python 3.11
- `poppler` installed on your system for PDF rendering
- A TikTok Shop Open Platform app
- A Telegram bot and chat ID

## Initial setup

### 1. Clone the repo and install dependencies

Open Anaconda Prompt and run:

```bash
conda create -n itbisa_order_bot python=3.11
conda activate itbisa_order_bot
conda install -c conda-forge poppler
cd C:\path\to\itbisa-tiktokshop-order-bot
python -m pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```env
TIKTOKSHOP_APP_KEY=your_app_key_here
TIKTOKSHOP_APP_SECRET=your_app_secret_here
TIKTOKSHOP_SHOP_ID=your_shop_id_here

TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=-100123456789
```

TikTok Shop access and refresh tokens are not environment variables. They are
stored in `data/tiktokshop_tokens.json` because the bot must rotate and persist
them automatically.

### 3. Bootstrap the TikTok Shop tokens one time

1. Log in to TikTok Shop Open Platform.
2. Authorize the app for your shop.
3. Copy the authorization code from the redirect URL.
4. Run:

```bash
python scripts/bootstrap_tokens.py
```

5. Paste the authorization code when prompted. The script writes
   `data/tiktokshop_tokens.json`.

## Running locally

After token bootstrap is complete, run:

```bash
python -m src.main
```

The bot queries your real TikTok Shop, processes actual packages, downloads
shipping labels, converts them to images, and sends them to Telegram.

## Production deployment with GitHub Actions

Go to **Settings → Secrets and variables → Actions** and add these secrets:

- `TIKTOKSHOP_APP_KEY`
- `TIKTOKSHOP_APP_SECRET`
- `TIKTOKSHOP_SHOP_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `STOCK_DISPATCH_TOKEN` (PAT used to dispatch the stock bot's `/stock_balance`)

### Runtime state branch

This repo uses two branches:

- **main** holds source code, workflow, scripts, and documentation.
- **bot-state** holds runtime state/token files only:
    - `data/processed_orders.json`
    - `data/tiktokshop_tokens.json`
    - `data/balance_throttle.json`

The workflow checks out `main`, overlays the latest `data/` files from
`bot-state` if that branch exists, runs the bot, then commits updated state and
rotated tokens back to `bot-state`.

Do not protect `bot-state`. The bot must be able to push updated state/token
files after every run.

### First-ever bootstrap

For a brand-new production bot where `bot-state` does not exist yet:

1. Run `python scripts/bootstrap_tokens.py` locally to create
   `data/tiktokshop_tokens.json`.
2. Ensure `data/processed_orders.json` contains `{}`.
3. Push the initial runtime files once so the first workflow run can start.
4. Trigger the workflow manually from the Actions tab.
5. After the first successful run, treat `bot-state` as the runtime source of
   truth and avoid manually editing runtime files unless you are recovering
   from a problem.

For an existing production bot, do not overwrite `bot-state` when updating
code or README files.

### Verify the workflow runs

Go to the **Actions** tab, click **Run TikTok Shop Order Bot** in the sidebar,
then click **Run workflow** to trigger a manual test run. Watch the logs to
confirm everything works. You should see a heartbeat message arrive in your
Telegram chat within a minute.

The bot runs only when dispatched (manually or by the Telegram Worker); there is
no automatic schedule.

## Authentication and signing

TikTok Shop uses short-lived access tokens and refresh tokens. This repo stores
both in `data/tiktokshop_tokens.json` with these fields:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "access_token_expires_at": "...",
  "refresh_token_expires_at": "..."
}
```

During each run:

1. `src/tiktokshop_auth.py` checks whether the access token is still valid.
2. If the access token is near expiry, it refreshes the full token bundle using
   TikTok Shop's auth endpoint.
3. Refreshed tokens are saved immediately to `data/tiktokshop_tokens.json`.
4. The workflow commits the updated token file back to `bot-state`, even when a
   later package fails, because the commit step uses `if: always()`.

If the refresh token itself expires, the bot raises a clear error and forwards
that error to Telegram. Re-run `scripts/bootstrap_tokens.py` to re-authorize the
shop.

Open API calls use TikTok Shop signing:

- Auth calls are plain `GET` calls to `https://auth.tiktok-shops.com`.
- Open API calls go to `https://open-api.tiktokglobalshop.com`.
- Open API calls include `x-tts-access-token`.
- Signature excludes `sign`, excludes `access_token`, excludes empty values,
  sorts params by key, concatenates `key + value`, builds
  `path + params + raw body`, wraps it with `app_secret` at both ends, then
  signs with HMAC-SHA256.

## Triggering

The workflow is `workflow_dispatch`-only — there is **no cron schedule**. It runs
when triggered manually from the Actions tab or dispatched by the upstream
Telegram router. Treat `.github/workflows/run.yml` as the source of truth.

## Tests and CI

Unit tests cover the pure logic only — `balance_dispatcher` (`to_base_sku`,
dedup, best-effort no-token dispatch), `balance_throttle` (`merge_pending`,
`window_open`), and the `telegram_sender` caption helpers (`_mono`,
`build_caption` including multi-courier inline). Network/API calls and the
label flow are not unit-tested. Install the dev dependencies and run:

```powershell
pip install -r requirements-dev.txt
pytest -q
```

`.github/workflows/ci.yml` runs `pytest` on pull requests that touch
`src/`, `tests/`, `requirements*.txt`, `pytest.ini`, `conftest.py`, or the CI
workflow itself. It needs no secrets and never touches `bot-state`.

## What the warehouse sees in Telegram

For each new package, a label image arrives with a caption like:

```text
📦 576461413038785752
🚚 JNT Express

Barang:
  • 20 x ITBISA-LED-5MM-RED
  • 15 x ITBISA-LED-5MM-GREEN
```

The caption uses SKU instead of product name because TikTok Shop product names
can be long, while SKUs match warehouse picking and packing.

If one PDF has multiple pages, the pages are merged every 2 pages before being
sent. For example:

- 1-page PDF → 1 Telegram image
- 2-page PDF → 1 Telegram image
- 3-page PDF → 2 Telegram images
- 4-page PDF → 2 Telegram images

At the end of every run, a short heartbeat message appears:

- `✅ TikTok Shop - 10:00 - Tidak ada pesanan baru`
- `✅ TikTok Shop - 12:00 - 3 label terkirim`
- `⚠️ TikTok Shop - 14:00 - 2 terkirim, 1 gagal (akan dicoba lagi)`

## Troubleshooting

### No labels are sent, but the workflow succeeds

Check the heartbeat message. If it says there are no new orders, the bot did
not find any unprocessed package in `AWAITING_SHIPMENT` or
`AWAITING_COLLECTION`.

### Waybill is not ready

TikTok Shop may need a short delay after a package is moved to ready-to-ship.
The bot retries a few times in the same run. If the waybill is still not ready,
it skips the package and tries again on the next run.

### Telegram delivery fails

The package is not marked processed unless Telegram accepts every image for
that package. Failed packages remain unprocessed and will be retried on the
next run.

### Token refresh fails

If TikTok Shop rejects the refresh token, re-run:

```bash
python scripts/bootstrap_tokens.py
```

For an existing production bot, update the token file in `bot-state` carefully.
For a first-ever bootstrap, the workflow can fall back to the initial `data/`
files on `main` and then create/update `bot-state`.

## Cost

Free forever for the current usage pattern.

- GitHub Actions: 2000 free minutes per month on private repos. Each run is
  ~1 minute; total depends on how often the bot is dispatched.
- Telegram Bot API: free.
- TikTok Shop Open API: free.
- No always-on server costs.

## Operational notes

- Keep `main` focused on source code and documentation.
- Keep runtime state/token updates on `bot-state`.
- Do not enable branch protection on `bot-state`.
- Do not hardcode secrets in Python files, workflow files, or README examples.
- User-facing Telegram messages should stay in Bahasa Indonesia.
