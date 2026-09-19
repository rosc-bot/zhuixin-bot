import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_TG_ID = int(os.getenv("ADMIN_TG_ID", "0"))

# Database paths
TG_MESSAGES_DB_PATHS = [
    os.getenv("TG_MESSAGES_DB_PATH", "/app/data/tg_messages.db"),
    "/home/ubuntu/如昔项目归总/群聊监听服务/data/tg_messages.db",
]
LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", str(BASE_DIR / "data" / "watchlist.db"))

# PostgreSQL
PG_DSN = os.getenv("PG_DSN", "")

# Integration with tg-media-bot
INGEST_PUSH_URL = os.getenv("INGEST_PUSH_URL", "http://127.0.0.1:12082/api/internal/channel-ingest/push")
INGEST_PUSH_TOKEN = os.getenv("INGEST_PUSH_TOKEN", "")
INGEST_CHANNEL_ID = os.getenv("INGEST_CHANNEL_ID", "")

# TMDB API
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

# FrameHdr 资源站配置
FRAMEHDR_ENABLED = os.getenv("FRAMEHDR_ENABLED", "false").lower() in ("true", "1", "yes")
FRAMEHDR_BASE_URL = os.getenv("FRAMEHDR_BASE_URL", "https://framehdr.com")
FRAMEHDR_USERNAME = os.getenv("FRAMEHDR_USERNAME", "")
FRAMEHDR_PASSWORD = os.getenv("FRAMEHDR_PASSWORD", "")
FRAMEHDR_COOKIE_FILE = os.getenv("FRAMEHDR_COOKIE_FILE", str(BASE_DIR / "data" / "framehdr_cookies.json"))
