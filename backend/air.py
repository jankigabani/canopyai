"""
Smoke and air quality — the human-impact angle.

Open-Meteo's Air Quality API (free, no key) gives PM2.5 and US AQI for any
point, batched like weather.py. We watch a fixed set of Ontario cities so
Canopy can say "smoke from these fires is reaching Thunder Bay", which a
detection-only map never tells you.
"""
import time

import httpx

AIR_API = "https://air-quality-api.open-meteo.com/v1/air-quality"

# (name, lat, lon) — the cities people actually live in downwind of the boreal.
CITIES = [
    ("Thunder Bay", 48.38, -89.25),
    ("Kenora", 49.77, -94.49),
    ("Timmins", 48.48, -81.33),
    ("Sudbury", 46.49, -80.99),
    ("Sault Ste. Marie", 46.52, -84.33),
    ("North Bay", 46.31, -79.46),
    ("Ottawa", 45.42, -75.70),
    ("Toronto", 43.65, -79.38),
]


def _label(pm25):
    """US EPA-style PM2.5 buckets, plain words."""
    if pm25 is None:
        return None
    if pm25 <= 12:
        return "Good"
    if pm25 <= 35:
        return "Moderate"
    if pm25 <= 55:
        return "Unhealthy for sensitive groups"
    if pm25 <= 150:
        return "Unhealthy"
    return "Hazardous"


async def smoke_at(points):
    """
    points: list of (lat, lon). One batched call. Returns a list (same order)
    of dicts with current PM2.5 / AQI and the 48h peak, or None per point on
    failure — callers degrade gracefully.
    """
    if not points:
        return []
    params = {
        "latitude": ",".join(str(round(la, 3)) for la, _ in points),
        "longitude": ",".join(str(round(lo, 3)) for _, lo in points),
        "current": "pm2_5,us_aqi",
        "hourly": "pm2_5",
        "forecast_days": 2,
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(AIR_API, params=params)
        data = resp.json()
    except (httpx.RequestError, ValueError):
        return [None] * len(points)

    locations = data if isinstance(data, list) else [data]
    out = []
    for loc in locations:
        if not isinstance(loc, dict) or "current" not in loc:
            out.append(None)
            continue
        cur = loc.get("current", {})
        hours = (loc.get("hourly", {}) or {}).get("pm2_5") or []
        peak = max((v for v in hours if v is not None), default=None)
        pm = cur.get("pm2_5")
        out.append(
            {
                "pm25_now": pm,
                "aqi_now": cur.get("us_aqi"),
                "pm25_peak": peak,
                "label": _label(pm),
                "peak_label": _label(peak),
            }
        )
    while len(out) < len(points):
        out.append(None)
    return out


_city_cache = {"at": 0.0, "data": None}


async def city_smoke():
    """Smoke picture for the watched cities, worst first. Cached ~10 min."""
    now = time.monotonic()
    if _city_cache["data"] is not None and now - _city_cache["at"] < 600:
        return _city_cache["data"]
    readings = await smoke_at([(la, lo) for _, la, lo in CITIES])
    cities = []
    for (name, la, lo), r in zip(CITIES, readings):
        if r:
            cities.append({"city": name, "lat": la, "lon": lo, **r})
    cities.sort(key=lambda c: c["pm25_now"] or 0, reverse=True)
    _city_cache.update(at=now, data=cities)
    return cities
