"""
Region-based change grid + predictive risk-forecast zones.

Two views the dashboard renders instead of plain dots:

  • change grid  — bins fire detections into map cells and colors each by the
    today-vs-yesterday change (red = flaring up, green = cooling down). This is
    the "region-based change" visualization.
  • risk forecast — combines recent fire density with tomorrow's Fire Weather
    Risk (from weather.py) to highlight cells likely to burn next.
"""
import math

from . import config, weather


def _cell_key(lat, lon, size):
    return (math.floor(lat / size), math.floor(lon / size))


def _cell_bounds(iy, ix, size):
    s = iy * size
    w = ix * size
    return {"south": round(s, 4), "west": round(w, 4),
            "north": round(s + size, 4), "east": round(w + size, 4),
            "lat": round(s + size / 2, 4), "lon": round(w + size / 2, 4)}


def change_grid(today_fires, yesterday_fires, size=None):
    """
    Bin today's and yesterday's fires into cells; return only non-empty cells
    with their counts and net change. Drives the colored region overlay.
    """
    size = size or config.GRID_CELL_DEG
    cells = {}

    def bump(fires, field):
        for f in fires:
            k = _cell_key(f["lat"], f["lon"], size)
            cell = cells.setdefault(k, {"today": 0, "yesterday": 0, "frp": 0.0})
            cell[field] += 1
            if field == "today" and f.get("frp"):
                cell["frp"] += f["frp"]

    bump(yesterday_fires, "yesterday")
    bump(today_fires, "today")

    out = []
    for (iy, ix), c in cells.items():
        b = _cell_bounds(iy, ix, size)
        out.append(
            {
                **b,
                "today": c["today"],
                "yesterday": c["yesterday"],
                "change": c["today"] - c["yesterday"],
                "total_frp": round(c["frp"], 1),
            }
        )
    out.sort(key=lambda c: abs(c["change"]), reverse=True)
    return out


async def risk_forecast(recent_fires, size=0.5, max_cells=25):
    """
    Predict where fire is likely next: take the cells with the most recent
    activity, fetch tomorrow's fire-weather risk for each, and blend them.
    Returns risk cells sorted high -> low. (size coarser than the change grid;
    capped to keep the Open-Meteo call light.)
    """
    density = {}
    for f in recent_fires:
        k = _cell_key(f["lat"], f["lon"], size)
        density[k] = density.get(k, 0) + 1
    if not density:
        return []

    top = sorted(density.items(), key=lambda kv: kv[1], reverse=True)[:max_cells]
    max_density = top[0][1]

    points = [(_cell_bounds(iy, ix, size)["lat"], _cell_bounds(iy, ix, size)["lon"])
              for (iy, ix), _ in top]
    wx = await weather.fire_weather_risk(points)

    out = []
    for ((iy, ix), count), w in zip(top, wx):
        b = _cell_bounds(iy, ix, size)
        density_norm = count / max_density           # 0..1
        weather_norm = (w["risk"] if w else 0) / 100  # 0..1
        # Forecast = half "it's already active here", half "conditions favor fire".
        forecast = round(100 * (0.5 * density_norm + 0.5 * weather_norm))
        out.append(
            {
                **b,
                "recent_fires": count,
                "weather_risk": w["risk"] if w else None,
                "weather_label": w["label"] if w else None,
                "temp_c": w["temp_c"] if w else None,
                "wind_kmh": w["wind_kmh"] if w else None,
                "humidity_pct": w["humidity_pct"] if w else None,
                "forecast_risk": forecast,
            }
        )
    out.sort(key=lambda c: c["forecast_risk"], reverse=True)
    return out
