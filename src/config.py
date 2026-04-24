"""
config.py
---------
Reads all secrets and settings from environment variables.

Why this file exists:
  We never hardcode secrets in the code. Instead, GitHub Actions injects them
  as environment variables at runtime, and this file is the ONLY place that
  reads them. Every other module gets these values by importing from here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


# STEP 0: Load .env file if it exists (local dev only). No-op in GitHub Actions.
load_dotenv()


# STEP 1: Project root. Absolute paths so data/ resolves consistently
# regardless of where Python was launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# STEP 2: Fake mode switch. When true, the TikTok Shop credentials are optional
# because we never call the real API.
USE_FAKE_TIKTOKSHOP = os.environ.get("USE_FAKE_TIKTOKSHOP", "false").lower() == "true"


# STEP 3: TikTok Shop API credentials. Kept as strings because TikTok Shop
# signing is string-based and some IDs are large.
if USE_FAKE_TIKTOKSHOP:
    TIKTOKSHOP_APP_KEY = os.environ.get("TIKTOKSHOP_APP_KEY", "")
    TIKTOKSHOP_APP_SECRET = os.environ.get("TIKTOKSHOP_APP_SECRET", "")
    TIKTOKSHOP_SHOP_ID = os.environ.get("TIKTOKSHOP_SHOP_ID", "")
else:
    TIKTOKSHOP_APP_KEY = os.environ["TIKTOKSHOP_APP_KEY"]
    TIKTOKSHOP_APP_SECRET = os.environ["TIKTOKSHOP_APP_SECRET"]
    TIKTOKSHOP_SHOP_ID = os.environ["TIKTOKSHOP_SHOP_ID"]


# STEP 4: Telegram bot credentials. Always required (even in fake mode).
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# STEP 5: Behavior constants and file paths.
#
# Two different TikTok Shop hosts are used:
#   - TIKTOKSHOP_AUTH_BASE_URL: token bootstrap + refresh
#   - TIKTOKSHOP_OPEN_API_BASE_URL: everything else (shops, orders, fulfillment)
TIKTOKSHOP_AUTH_BASE_URL = "https://auth.tiktok-shops.com"
TIKTOKSHOP_OPEN_API_BASE_URL = "https://open-api.tiktokglobalshop.com"

# API version used in the URL path (e.g. /order/202309/orders/search).
TIKTOKSHOP_API_VERSION = "202309"

# Document type passed to Get Package Shipping Document. PACKING_SLIP matches
# what the verified curl example used.
TIKTOKSHOP_DOCUMENT_TYPE = "PACKING_SLIP"

STATE_FILE_PATH = str(PROJECT_ROOT / "data" / "processed_orders.json")
TOKENS_FILE_PATH = str(PROJECT_ROOT / "data" / "tiktokshop_tokens.json")

MAX_ORDERS_PER_RUN = 30       # Safety cap. If we see more than this, stop.
LABEL_IMAGE_DPI = 200         # Resolution for PDF -> PNG conversion.
STATE_RETENTION_DAYS = 5      # How long to remember processed packages.
TOKEN_REFRESH_BUFFER_MINUTES = 10  # Refresh access_token N min before expiry.