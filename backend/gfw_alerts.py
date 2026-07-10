"""
Current-year disturbance alerts — the "what is happening in 2026" numbers.

The Hansen loss layer stops at the last annual release (2024). This module
queries the GFW Data API's DIST-ALERT raster (OPERA/UMD, global, updated
every 2-4 days) for dated alerts since January 1 of the current year and
rolls them up weekly and monthly. That gives the dashboard a live "this
year, this week" deforestation signal no annual dataset can.

Queries over big regions take 10-60s, so results are cached hard (6h) and
the scheduler pre-warms Ontario.
"""
import logging
import time
from datetime import date, datetime, timedelta

import httpx

from . import config

log = logging.getLogger("forest-watch")

DATASET = "umd_glad_dist_alerts"
QUERY_URL = f"https://data-api.globalforestwatch.org/dataset/{DATASET}/latest/query/json"

_CACHE_TTL = 6 * 3600
_cache = {}  # bbox key -> {"at": monotonic, "data": {...}}


def _key(bbox):
    return tuple(round(v, 2) for v in bbox)


def _bbox_polygon(bbox):
    w, s, e, n = bbox
    return {
        "type": "Polygon",
        "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
    }


def _week_start(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def _aggregate(rows):
    """Daily rows -> weekly + monthly series, totals, and 7-day momentum."""
    weekly, monthly = {}, {}
    total = 0.0
    latest = None
    today = datetime.utcnow().date()
    last7 = prev7 = 0.0

    for row in rows:
        ds, area = row.get("date"), row.get("area") or 0.0
        if not ds:
            continue
        d = date.fromisoformat(ds)
        total += area
        weekly[_week_start(d)] = weekly.get(_week_start(d), 0.0) + area
        monthly[ds[:7]] = monthly.get(ds[:7], 0.0) + area
        if latest is None or ds > latest:
            latest = ds
        age = (today - d).days
        if age <= 7:
            last7 += area
        elif age <= 14:
            prev7 += area

    return {
        "year": today.year,
        "total_ha": round(total),
        "latest_date": latest,
        "last7_ha": round(last7),
        "prev7_ha": round(prev7),
        "wow_pct": round((last7 - prev7) / prev7 * 100) if prev7 else None,
        "weekly": [
            {"week": wk, "area_ha": round(a)} for wk, a in sorted(weekly.items())
        ],
        "monthly": [
            {"month": m, "area_ha": round(a)} for m, a in sorted(monthly.items())
        ],
        "source": "OPERA DIST-ALERT via GFW Data API",
        "live": True,
    }


def get_cached(bbox):
    """Cached result only, never blocks. For chat context and alerts."""
    hit = _cache.get(_key(bbox))
    return hit["data"] if hit else None


async def alerts_this_year(bbox, timeout=150, force=False):
    """
    Weekly/monthly disturbance-alert areas since Jan 1 of the current year for
    a bbox. Returns the aggregate dict, or None if not configured / failed.
    Cached for 6h (queries are heavy — Ontario takes ~20s). `force` skips the
    cache read so the scheduler can compare fresh passes.
    """
    if not config.GFW_API_KEY:
        return None

    k = _key(bbox)
    hit = _cache.get(k)
    if hit and not force and time.monotonic() - hit["at"] < _CACHE_TTL:
        return hit["data"]

    year = datetime.utcnow().year
    sql = (
        f"SELECT {DATASET}__date AS date, SUM(area__ha) AS area "
        "FROM results "
        f"WHERE {DATASET}__date >= '{year}-01-01' "
        f"GROUP BY {DATASET}__date "
        f"ORDER BY {DATASET}__date"
    )
    payload = {"sql": sql, "geometry": _bbox_polygon(bbox)}
    headers = {"x-api-key": config.GFW_API_KEY, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(QUERY_URL, json=payload, headers=headers)
        if resp.status_code != 200:
            log.warning("DIST-ALERT query failed: %s %s", resp.status_code, resp.text[:150])
            return None
        rows = resp.json().get("data", [])
    except (httpx.RequestError, ValueError) as exc:
        log.warning("DIST-ALERT query error: %s", exc)
        return None

    data = _aggregate(rows) if rows else None
    if data:
        _cache[k] = {"at": time.monotonic(), "data": data}
    return data
