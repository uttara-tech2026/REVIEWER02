import os
from dotenv import load_dotenv

load_dotenv()

# =========================
# REQUIRED SETTINGS
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is missing in .env file")

# =========================
# SCHEDULER DEFAULTS
# (per-chat settings override these; these are only the
#  factory defaults used the first time a chat is seen)
# =========================

# "fixed" | "random"
SCHEDULE_MODE = os.getenv("SCHEDULE_MODE", "fixed")

DEFAULT_SEND_DELAY = int(os.getenv("DEFAULT_SEND_DELAY", "180"))

MIN_SEND_DELAY = int(os.getenv("MIN_SEND_DELAY", "120"))
MAX_SEND_DELAY = int(os.getenv("MAX_SEND_DELAY", "300"))

# =========================
# QUEUE / UPLOAD DEFAULTS
# =========================

MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "1000"))

ALLOWED_FILE_TYPES = os.getenv("ALLOWED_FILE_TYPES", "photo,video").split(",")

DEFAULT_CAPTION = os.getenv("DEFAULT_CAPTION", "")

# =========================
# LOCALE
# =========================

SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "Asia/Kolkata")

DATETIME_FORMAT = os.getenv("DATETIME_FORMAT", "%Y-%m-%d %H:%M:%S")

# =========================
# STORAGE
# =========================

DATA_FILE = os.getenv("DATA_FILE", "data.json")

# Where extracted zip media is temporarily kept
MEDIA_DIR = os.getenv("MEDIA_DIR", "media_cache")
