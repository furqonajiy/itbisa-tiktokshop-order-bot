"""
config.py
---------
Reads secrets and settings from environment variables. Every other module
imports from here so we have a single source of truth.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file if present (local dev only). No-op on GitHub Actions.
load_dotenv()


# Absolute project root so data/ resolves regardless of where Python was launched.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# TikTok Shop API credentials.
TIKTOKSHOP_APP_KEY = os.environ["TIKTOKSHOP_APP_KEY"]
TIKTOKSHOP_APP_SECRET = os.environ["TIKTOKSHOP_APP_SECRET"]
TIKTOKSHOP_SHOP_ID = os.environ["TIKTOKSHOP_SHOP_ID"]


# Telegram bot credentials.
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# TikTok Shop hosts.
#   AUTH_BASE_URL     -> token bootstrap + refresh
#   OPEN_API_BASE_URL -> everything else (shops, orders, fulfillment)
TIKTOKSHOP_AUTH_BASE_URL = "https://auth.tiktok-shops.com"
TIKTOKSHOP_OPEN_API_BASE_URL = "https://open-api.tiktokglobalshop.com"

# Document type passed to Get Package Shipping Document.
TIKTOKSHOP_DOCUMENT_TYPE = "SHIPPING_LABEL"


# File paths for runtime state.
STATE_FILE_PATH = str(PROJECT_ROOT / "data" / "processed_orders.json")
TOKENS_FILE_PATH = str(PROJECT_ROOT / "data" / "tiktokshop_tokens.json")


# Behavior constants.
MAX_ORDERS_PER_RUN = 30             # Safety cap; stop and alert if exceeded.
LABEL_IMAGE_DPI = 200               # Resolution for PDF -> PNG conversion.
STATE_RETENTION_DAYS = 2            # How long to remember processed packages.
TOKEN_REFRESH_BUFFER_MINUTES = 10   # Refresh access_token N min before expiry.