# ITBisa TikTok Shop Order Bot

Automatically fetches new TikTok Shop orders, downloads shipping labels,
converts them to images, and sends them to a Telegram bot so the warehouse
employee can print them from a phone without manual downloading.

## What it does

Every day on a fixed Jakarta-time schedule, GitHub Actions runs a Python
script that:

1. Asks TikTok Shop for recent orders.
2. Keeps only orders that still need fulfillment attention.
3. Skips any orders already processed in a previous run.
4. For each new order:
   - If still `AWAITING_SHIPMENT`, calls TikTok Shop's ready-to-ship API.
   - Downloads the shipping label PDF.
   - Converts the PDF to one or more PNG images.
   - Sends the result to a Telegram chat with a caption describing the order
     in Bahasa Indonesia.
5. Sends a heartbeat summary at the end of every run so the employee knows
   the bot is alive, even when no new orders came in.
6. Remembers what was processed by committing small JSON files back to the
   repository.

Everything runs on the GitHub Actions free tier. There is no server, no
cloud VM, and no database.

## Project structure

```text
itbisa-tiktokshop-order-bot/
├── .github/workflows/
│   └── run.yml                      # GitHub Actions schedule + bot-state sync
├── data/                            # Runtime state used for bootstrap and bot-state sync
│   ├── processed_orders.json        # Which orders we already sent to Telegram
│   └── tiktokshop_tokens.json       # Current access + refresh tokens
├── scripts/
│   ├── bootstrap_tokens.py          # One-time script to seed tiktokshop_tokens.json
│   ├── update_inventory.py          # Utility to update Shopee stock from Excel
│   └── test_*.py                    # Helper / diagnostic scripts for API testing
├── src/
│   ├── __init__.py
│   ├── main.py                      # Entry point, orchestrates the flow
│   ├── config.py                    # Reads secrets + settings from env
│   ├── tiktokshop_auth.py           # Token lifecycle
│   ├── tiktokshop_client.py         # TikTok Shop API calls + signing
│   ├── tiktokshop_client_fake.py    # Canned fake data for local testing
│   ├── label_processor.py           # PDF → PNG conversion + bottom whitespace crop
│   ├── telegram_sender.py           # Sends images + messages in Bahasa
│   └── state_manager.py             # Loads/saves processed_orders.json
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

USE_FAKE_TIKTOKSHOP=false
```

TikTok Shop tokens are managed in `data/tiktokshop_tokens.json` because they
must be refreshed automatically.

### 3. Bootstrap the TikTok Shop tokens (one-time)

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

### Test with fake data

Set `USE_FAKE_TIKTOKSHOP=true` in `.env` and run:

```bash
python -m src.main
```

### Test with real TikTok Shop

Set `USE_FAKE_TIKTOKSHOP=false` and run:

```bash
python -m src.main
```

## Production deployment (GitHub Actions)

Go to **Settings → Secrets and variables → Actions** and add these secrets:

- `TIKTOKSHOP_APP_KEY`
- `TIKTOKSHOP_APP_SECRET`
- `TIKTOKSHOP_SHOP_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 3. Push the initial state files

The first workflow run still expects the initial state files to be
available from `main`. Push them once:

```bash
git add data/tiktokshop_tokens.json data/processed_orders.json
git commit -m "Bootstrap initial state files"
git push
```

After the first successful workflow run, ongoing state updates are pushed
to the `bot-state` branch. The `bot-state` branch should be treated as the
source of truth for runtime state.

### 4. Verify the workflow runs

Go to the **Actions** tab, click **Run Shopee Order Bot** in the sidebar,
then click **Run workflow** to trigger a manual test run. Watch the logs
to confirm everything works. You should see a heartbeat message arrive
in your Telegram chat within a minute.

After the first successful manual run, the schedule takes over and runs the
bot automatically.

## State management (the `bot-state` branch)

This repo uses two branches:

- **main** holds the source code.
- **bot-state** holds the runtime state files (`processed_orders.json` and
  `tiktokshop_tokens.json`).

The workflow checks out `main`, overlays the latest `data/` files from
`bot-state` if that branch exists, runs the bot, then commits updated
state back to `bot-state`.

For the very first run, the workflow falls back to the `data/` files
already present on `main`, then creates or updates `bot-state`.

### Bootstrapping for the first time

When you initially set up the bot:

1. Run `python scripts/bootstrap_tokens.py` locally to create
   `data/tiktokshop_tokens.json`.
2. Push your code to `main` (with `data/processed_orders.json` as `{}` and
   `data/tiktokshop_tokens.json` from the bootstrap).
3. Trigger the workflow manually from the Actions tab.
4. The first run will create or update the `bot-state` branch and push the
   state files there. From then on, runtime updates go to `bot-state`.

### What you should NOT do

- Do not delete the `bot-state` branch unless you want to lose the bot's
  memory of which orders were already processed and which token is current.
- Do not enable branch protection on `bot-state`. It is supposed to be
  bot-writable.
- Do not manually edit files on `bot-state` unless you are recovering
  from a problem. Any manual edit is at risk of being overwritten by the
  next scheduled run.

## How authentication works

TODO

## Daily schedule

The GitHub Actions workflow currently runs **4 times per day** based on
Jakarta time (WIB = UTC+7):

- **05:00 WIB**
- **10:00 WIB**
- **14:00 WIB**
- **17:00 WIB**

In cron syntax (GitHub Actions uses UTC), this is:

```text
0 22,3,7,10 * * *
```

## What your employee sees in Telegram

For each new order, a label image arrives with a caption like:

```text
📦 576461413038785752
🚚 JNT Express

Barang:
  • 20 x ITBISA-LED-5MM-RED
  • 15 x ITBISA-LED-5MM-GREEN
```

The caption uses SKU instead of the product name because product names
on Shopee are very long, while SKUs are short and match how the warehouse
is organized. When a product has variants, the variant SKU is shown.
When a product has no variants, the main product SKU is shown.

At the end of every run, a short heartbeat message appears:

- `✅ 11:00 - Tidak ada pesanan baru` (quiet hour)
- `✅ 12:00 - 3 label terkirim` (normal hour)
- `⚠️ 13:00 - 2 terkirim, 1 gagal (akan dicoba lagi)` (problem hour)

If the refresh token expires, a rare manual-intervention alert appears.

## Utility scripts

### `scripts/update_inventory.py`

This script updates Shopee inventory from an Excel file.

Usage:

```bash
python scripts/update_inventory.py path/to/tiktokshop_inventory.xlsx
```

Expected Excel columns:

- `SKU`
- `Stock`

It fetches your product catalog, maps SKUs to Shopee item/model IDs, then
updates stock one SKU at a time with a small delay to stay polite to
Shopee's rate limits.

## Troubleshooting

TODO

## Cost

Free forever.

- GitHub Actions: 2000 free minutes per month on private repos. The bot
  uses roughly 4 runs/day × ~1 minute = about 120 minutes/month.
- Telegram Bot API: free.
- Shopee Open API: free.
- No always-on server costs.

## Important note

This code keeps the **same bot flow and architecture** as the original Shopee
version and only swaps the marketplace integration to TikTok Shop. That makes
it easier to review, easier to diff, and safer to hand over to junior engineers.
