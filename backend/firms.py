"""
NASA FIRMS data access + aggregation.

One place that knows how to fetch active-fire detections and turn the raw CSV
into clean Python dicts, plus helpers to aggregate those into the stats the
dashboard shows. No local storage — every call hits FIRMS live.
"""
import csv
import io
from collections import Counter, defaultdict
from datetime import date, timedelta

import httpx

from . import config

FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_fires(bbox, days=1, source="VIIRS_NOAA20_NRT", date_=None):
    """
    Fetch fire detections for a bbox tuple (w,s,e,n) or the string 'world'.

    Returns a list of fire dicts. Raises RuntimeError with a readable message
    if FIRMS rejects the request (bad key, etc.).
    """
    if not config.FIRMS_MAP_KEY:
        raise RuntimeError(
            "FIRMS_MAP_KEY is not set. Copy .env.example to .env and add your key."
        )

    area = bbox if isinstance(bbox, str) else config.bbox_str(bbox)
    url = f"{FIRMS_BASE}/{config.FIRMS_MAP_KEY}/{source}/{area}/{days}"
    if date_:
        url += f"/{date_}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)

    text = resp.text
    first_line = text.split("\n", 1)[0].lower()
    if resp.status_code != 200 or "latitude" not in first_line:
        raise RuntimeError(f"FIRMS error: {text[:200]}")

    fires = []
    for row in csv.DictReader(io.StringIO(text)):
        lat = _to_float(row.get("latitude"))
        lon = _to_float(row.get("longitude"))
        if lat is None or lon is None:
            continue
        fires.append(
            {
                "lat": lat,
                "lon": lon,
                "brightness": _to_float(row.get("bright_ti4") or row.get("brightness")),
                "frp": _to_float(row.get("frp")),
                "confidence": row.get("confidence"),
                "acq_date": row.get("acq_date"),
                "acq_time": row.get("acq_time"),
                "satellite": row.get("satellite"),
                "daynight": row.get("daynight"),
                "province": config.province_for(lat, lon),
            }
        )
    return fires


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def summarize(fires):
    """Headline numbers for a set of fires (used by stat cards)."""
    frps = [f["frp"] for f in fires if f["frp"] is not None]
    return {
        "count": len(fires),
        "avg_frp": _avg(frps),
        "max_frp": round(max(frps), 1) if frps else 0.0,
        "high_intensity": sum(1 for v in frps if v >= 30),
    }


def by_province(fires):
    """Fire counts grouped by province label (skips points outside Canada)."""
    counts = Counter(f["province"] for f in fires if f["province"])
    return dict(counts.most_common())


def daily_trend(fires):
    """
    Fire counts per acquisition date, returned oldest -> newest.
    Drives the 7-day trend chart.
    """
    counts = defaultdict(int)
    for f in fires:
        if f["acq_date"]:
            counts[f["acq_date"]] += 1
    return [{"date": d, "count": counts[d]} for d in sorted(counts)]


def split_today_yesterday(fires):
    """
    Split a multi-day fire list into (today, yesterday) buckets by acq_date.
    'Today' = the most recent date present in the data (FIRMS lags a few hours,
    so the latest available date is what we treat as 'current').
    """
    dates = sorted({f["acq_date"] for f in fires if f["acq_date"]})
    if not dates:
        return [], []
    today = dates[-1]
    yest = dates[-2] if len(dates) > 1 else None
    today_fires = [f for f in fires if f["acq_date"] == today]
    yest_fires = [f for f in fires if f["acq_date"] == yest] if yest else []
    return today_fires, yest_fires
