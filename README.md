# ITBisa TikTok Shop Order Bot

Automatically processes TikTok Shop packages that need shipping labels,
downloads the package shipping documents, converts them to Telegram-friendly
PNG images, and sends them to the warehouse Telegram chat so labels can be
printed from a phone without manual downloading.

Everything runs as a short-lived GitHub Actions job. There is no server, no
cloud VM, and no database.

## Real production flow

Every scheduled or manually triggered run does one processing cycle:

1. GitHub Actions checks out `main` for source code.
2. The workflow overlays `data/processed_orders.json` and
   `data/tiktokshop_tokens.json` from the `bot-state` branch when available.
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
17. At the end, the bot sends a heartbeat summary to Telegram.
18. The workflow commits updated runtime state and rotated tokens back to
    `bot-state`.

The tracking unit is `package_id`, not `order_id`, because one TikTok Shop
order can contain multiple packages and each package has its own label.

## Project structure

```text
itbisa-tiktokshop-order-bot/
├── .github/workflows/
│   └── run.yml                      # GitHub Actions schedule + bot-state sync
├── data/                            # Runtime state bootstrap files
│   ├── processed_orders.json        # package_id values already sent to Telegram
│   └── tiktokshop_tokens.json       # access_token + refresh_token bundle
├── scripts/
│   ├── bootstrap_tokens.py          # One-time / recovery token bootstrap
│   └── get_chiper_code.py           # Diagnostic helper; name kept as-is
├── src/
│   ├── __init__.py
│   ├── main.py                      # Entry point, orchestrates one run
│   ├── config.py                    # Env vars, API hosts, paths, constants
│   ├── tiktokshop_auth.py           # Token lifecycle + refresh-token expiry guard
│   ├── tiktokshop_client.py         # TikTok Shop Open API calls + signing
│   ├── label_processor.py           # PDF → PNG conversion + 2-page merge
│   ├── telegram_sender.py           # Telegram labels + Bahasa status messages
│   └── state_manager.py             # Loads/saves processed package state
├── requirements.txt
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

### Runtime state branch

This repo uses two branches:

- **main** holds source code, workflow, scripts, and documentation.
- **bot-state** holds runtime state/token files only:
    - `data/processed_orders.json`
    - `data/tiktokshop_tokens.json`

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

After the first successful manual run, the schedule takes over and runs the
bot automatically.

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

## Daily schedule

The GitHub Actions workflow currently runs **5 times per day** based on
Jakarta time (WIB = UTC+7):

- **10:00 WIB**
- **12:00 WIB**
- **14:00 WIB**
- **16:00 WIB**
- **18:00 WIB**

In cron syntax, because GitHub Actions uses UTC, this is:

```text
0 3,5,7,9,11 * * *
```

The workflow also supports `workflow_dispatch`, so it can be triggered manually
from GitHub Actions or by the upstream Telegram router.

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
it skips the package and tries again in the next scheduled run.

### Telegram delivery fails

The package is not marked processed unless Telegram accepts every image for
that package. Failed packages remain unprocessed and will be retried on the
next scheduled run.

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

- GitHub Actions: 2000 free minutes per month on private repos. The bot uses
  roughly 5 runs/day × ~1 minute = about 150 minutes/month.
- Telegram Bot API: free.
- TikTok Shop Open API: free.
- No always-on server costs.

## Operational notes

- Keep `main` focused on source code and documentation.
- Keep runtime state/token updates on `bot-state`.
- Do not enable branch protection on `bot-state`.
- Do not hardcode secrets in Python files, workflow files, or README examples.
- User-facing Telegram messages should stay in Bahasa Indonesia.
