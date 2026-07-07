"""
Configuration + runtime state for Forest Watch.

Holds region definitions (Ontario / Canada / provinces), default thresholds,
and a small in-memory settings object you can change at runtime via the API
(phone/chat id, alert threshold). Nothing here needs a database — we keep the
live config in memory and seed it from the .env file on startup.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Secrets / keys (from .env) ---------------------------------------------
FIRMS_MAP_KEY = os.getenv("FIRMS_MAP_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()  # optional, for AI briefings
GFW_API_KEY = os.getenv("GFW_API_KEY", "").strip()  # optional, for live GFW loss stats

# Grid cell size in degrees for the region-change visualization (~33 km).
GRID_CELL_DEG = 0.3

# GFW tile settings (proxied to dodge CORS + follow redirects).
GFW_TILES = "https://tiles.globalforestwatch.org"
GFW_LOSS_VERSION = "v1.11"
GFW_ALERTS_VERSION = "latest"
ALLOWED_TCD = {"tcd_10", "tcd_15", "tcd_20", "tcd_25", "tcd_30", "tcd_50", "tcd_75"}

# --- Regions ----------------------------------------------------------------
# Bounding boxes as (west, south, east, north). FIRMS wants this exact order.
ONTARIO_BBOX = (-95.2, 41.6, -74.3, 56.9)
CANADA_BBOX = (-141.0, 41.7, -52.6, 83.1)

# Rough province boxes used to label points for the dashboard breakdown.
# These overlap slightly at borders; a point is assigned to the FIRST match,
# so order matters (more specific / eastern provinces first is fine here).
PROVINCE_BBOXES = {
    "Ontario": (-95.2, 41.6, -74.3, 56.9),
    "Quebec": (-79.8, 44.9, -57.0, 62.6),
    "British Columbia": (-139.1, 48.2, -114.0, 60.0),
    "Alberta": (-120.0, 48.9, -110.0, 60.0),
    "Saskatchewan": (-110.0, 48.9, -101.4, 60.0),
    "Manitoba": (-102.0, 48.9, -88.9, 60.0),
}


def bbox_str(bbox):
    """Turn a (w, s, e, n) tuple into the 'w,s,e,n' string FIRMS expects."""
    return ",".join(str(round(v, 4)) for v in bbox)


def point_in_bbox(lat, lon, bbox):
    w, s, e, n = bbox
    return s <= lat <= n and w <= lon <= e


def province_for(lat, lon):
    """Best-effort province label for a fire point (or None if outside)."""
    for name, bbox in PROVINCE_BBOXES.items():
        if point_in_bbox(lat, lon, bbox):
            return name
    return None


# --- Runtime alert settings (mutable, changed via /api/alert/config) --------
class AlertSettings:
    def __init__(self):
        # Telegram chat id the alerts get sent to. Seed from env if present.
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
        # Minimum number of *new* fire clusters in Ontario to trigger an alert.
        self.new_cluster_threshold = 1
        # Also alert if today's count is this many std-devs above the 7-day mean.
        self.anomaly_zscore_threshold = 2.0
        # Master switch for the hourly automated checker.
        self.auto_alerts_enabled = True

    def as_dict(self):
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "new_cluster_threshold": self.new_cluster_threshold,
            "anomaly_zscore_threshold": self.anomaly_zscore_threshold,
            "auto_alerts_enabled": self.auto_alerts_enabled,
            "telegram_bot_configured": bool(TELEGRAM_BOT_TOKEN),
            "firms_key_configured": bool(FIRMS_MAP_KEY),
            "ai_configured": bool(GROQ_API_KEY),
            "gfw_configured": bool(GFW_API_KEY),
        }


# Single shared instance imported across the app.
settings = AlertSettings()

# In-memory log of alerts we've sent (newest first). Capped to keep it small.
ALERT_LOG = []


def record_alert(entry: dict):
    ALERT_LOG.insert(0, entry)
    del ALERT_LOG[50:]
