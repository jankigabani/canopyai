"""
Weather + Fire Weather Risk — the predictive signal.

Pulls a short forecast from Open-Meteo (free, no API key) for a batch of points
in a single request, and turns each into a 0-100 Fire Weather Risk index from
the classic fire-danger drivers: hot, dry, windy, no rain = high risk.

This is what lets Forest Watch say "where is likely to burn *next*", which a
detection-only map (like Global Forest Watch) can't.
"""
import httpx

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def _risk_index(temp_c, humidity_pct, wind_kmh, precip_mm):
    """Combine fire-danger drivers into a 0-100 score (transparent + tunable)."""
    if temp_c is None:
        return 0
    # Each component 0-1, higher = more dangerous.
    heat = max(0, min(1, (temp_c - 15) / 25))          # 15°C..40°C
    dry = max(0, min(1, (60 - (humidity_pct or 60)) / 50))  # 60%..10% RH
    wind = max(0, min(1, (wind_kmh or 0) / 40))         # 0..40 km/h
    rain = 1 - max(0, min(1, (precip_mm or 0) / 10))    # 0mm..10mm (inverted)
    score = 100 * (0.35 * heat + 0.30 * dry + 0.20 * wind + 0.15 * rain)
    return round(score)


def _label(score):
    if score >= 70:
        return "Extreme"
    if score >= 50:
        return "High"
    if score >= 30:
        return "Moderate"
    return "Low"


async def fire_weather_risk(points):
    """
    points: list of (lat, lon). Returns list of dicts with weather + risk for
    tomorrow. One batched Open-Meteo call for all points.
    """
    if not points:
        return []
    lats = ",".join(str(round(la, 3)) for la, _ in points)
    lons = ",".join(str(round(lo, 3)) for _, lo in points)
    params = {
        "latitude": lats,
        "longitude": lons,
        "daily": "temperature_2m_max,relative_humidity_2m_min,"
                 "wind_speed_10m_max,precipitation_sum",
        "forecast_days": 2,
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(OPEN_METEO, params=params)
        data = resp.json()
    except (httpx.RequestError, ValueError):
        return []

    # Open-Meteo returns a single object for one point, a list for many.
    locations = data if isinstance(data, list) else [data]
    out = []
    for (lat, lon), loc in zip(points, locations):
        daily = loc.get("daily", {}) if isinstance(loc, dict) else {}
        # index 1 = tomorrow (index 0 = today)
        def day(key):
            arr = daily.get(key) or []
            return arr[1] if len(arr) > 1 else (arr[0] if arr else None)

        temp = day("temperature_2m_max")
        rh = day("relative_humidity_2m_min")
        wind = day("wind_speed_10m_max")
        precip = day("precipitation_sum")
        score = _risk_index(temp, rh, wind, precip)
        out.append(
            {
                "lat": lat,
                "lon": lon,
                "risk": score,
                "label": _label(score),
                "temp_c": temp,
                "humidity_pct": rh,
                "wind_kmh": wind,
                "precip_mm": precip,
            }
        )
    return out
