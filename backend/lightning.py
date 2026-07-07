"""
Live lightning + ignition watch — alerts BEFORE the fire exists.

Blitzortung.org is a volunteer lightning detection network with a free live
websocket feed (non-commercial use). We keep a rolling window of strikes over
Canada, grid them, and cross the active cells with tomorrow's fire weather:
several strikes into a hot dry windy cell = an ignition watch, pushed to the
live feed and (throttled) to Telegram.

The feed protocol is unofficial. If it can't connect, everything degrades
gracefully: endpoints report available=false and the rest of the app is
untouched.
"""
import asyncio
import itertools
import json
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone

from . import alerts, config, live, weather

log = logging.getLogger("forest-watch")

WS_HOSTS = [
    "wss://ws1.blitzortung.org/",
    "wss://ws7.blitzortung.org/",
    "wss://ws8.blitzortung.org/",
]

# Rolling strike buffer: (epoch_seconds, lat, lon). Canada only.
STRIKES = deque(maxlen=20000)
STATE = {"connected": False, "total_canada": 0, "last_strike_at": None}

# Most recent ignition-watch cells (refreshed by the 5-minute job).
LAST_IGNITION = {"zones": [], "checked_at": None}


def _lzw_decode(data: str) -> str:
    """Blitzortung streams LZW-compressed JSON strings."""
    dict_ = {}
    result = [data[0]]
    prev = data[0]
    code = 256
    for ch in data[1:]:
        cur = ord(ch)
        entry = ch if cur < 256 else dict_.get(cur, prev + prev[0])
        result.append(entry)
        dict_[code] = prev + entry[0]
        code += 1
        prev = entry
    return "".join(result)


_last_ws_emit = 0.0


async def _maybe_broadcast(lat, lon):
    """Push strikes to the browser feed, throttled so storms don't flood it."""
    global _last_ws_emit
    now = time.time()
    if now - _last_ws_emit < 3:
        return
    _last_ws_emit = now
    await live.emit(
        "lightning",
        f"Lightning strike at {lat:.2f}, {lon:.2f}",
        lat=round(lat, 3),
        lon=round(lon, 3),
    )


async def run_client():
    """
    Background task: connect to a Blitzortung relay, collect Canada strikes,
    reconnect forever. Never raises — a dead feed just means no lightning data.
    """
    try:
        import websockets
    except ImportError:
        log.warning("websockets package missing, lightning feed disabled")
        return

    hosts = itertools.cycle(WS_HOSTS)
    while True:
        host = next(hosts)
        try:
            async with websockets.connect(host, open_timeout=15) as ws:
                await ws.send('{"a": 111}')
                STATE["connected"] = True
                log.info("Lightning feed connected: %s", host)
                async for raw in ws:
                    try:
                        msg = json.loads(_lzw_decode(raw))
                    except Exception:
                        continue
                    lat, lon = msg.get("lat"), msg.get("lon")
                    if lat is None or lon is None:
                        continue
                    if not config.point_in_bbox(lat, lon, config.CANADA_BBOX):
                        continue
                    STRIKES.append((time.time(), lat, lon))
                    STATE["total_canada"] += 1
                    STATE["last_strike_at"] = datetime.now(
                        timezone.utc
                    ).isoformat(timespec="seconds")
                    await _maybe_broadcast(lat, lon)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.info("Lightning feed dropped (%s): %s", host, exc)
        STATE["connected"] = False
        await asyncio.sleep(10)


def recent_strikes(minutes=60):
    """Strikes from the last N minutes, newest first."""
    now = time.time()
    cutoff = now - minutes * 60
    out = [
        {"lat": round(la, 3), "lon": round(lo, 3), "age_min": int((now - t) / 60)}
        for t, la, lo in STRIKES
        if t >= cutoff
    ]
    out.reverse()
    return out


async def ignition_watch(minutes=60, size=0.5, max_cells=12):
    """
    Cross recent strikes with tomorrow's fire weather. A cell that just took
    lightning AND is hot/dry/windy is where the next fire starts. Returns
    cells sorted by ignition risk.
    """
    strikes = recent_strikes(minutes)
    cells = {}
    for s in strikes:
        k = (math.floor(s["lat"] / size), math.floor(s["lon"] / size))
        cells[k] = cells.get(k, 0) + 1
    if not cells:
        return []

    top = sorted(cells.items(), key=lambda kv: kv[1], reverse=True)[:max_cells]
    points = [((iy + 0.5) * size, (ix + 0.5) * size) for (iy, ix), _ in top]
    wx = await weather.fire_weather_risk(points)

    out = []
    for ((iy, ix), count), w in zip(top, wx):
        risk = w["risk"] if w else 0
        # Half "how much lightning", half "how burnable is it there".
        score = min(100, round(50 * min(count, 10) / 10 + 0.5 * risk))
        out.append(
            {
                "lat": round((iy + 0.5) * size, 3),
                "lon": round((ix + 0.5) * size, 3),
                "strikes": count,
                "weather_risk": risk,
                "weather_label": w["label"] if w else None,
                "ignition_risk": score,
            }
        )
    out.sort(key=lambda c: c["ignition_risk"], reverse=True)
    return out


# One Telegram alert per cell per 6 hours, or storms would melt the phone.
_alerted_cells = {}


async def check_and_alert():
    """5-minute job: refresh ignition watch, push events, alert on hot cells."""
    zones = await ignition_watch()
    LAST_IGNITION["zones"] = zones
    LAST_IGNITION["checked_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    for z in zones:
        if z["strikes"] < 3 or (z["weather_risk"] or 0) < 50:
            continue
        key = (round(z["lat"], 1), round(z["lon"], 1))
        if time.time() - _alerted_cells.get(key, 0) < 6 * 3600:
            continue
        _alerted_cells[key] = time.time()
        label = (z["weather_label"] or "unknown").lower()
        text = (
            "⚡ <b>Canopy ignition watch.</b>\n\n"
            f"{z['strikes']} lightning strikes in the last hour near "
            f"({z['lat']}, {z['lon']}).\n"
            f"Fire weather there is {label} risk. No fire detected yet.\n"
            "We are watching this cell. If a hotspot shows up you will know first."
        )
        await live.emit(
            "ignition",
            f"{z['strikes']} strikes into a {label} risk zone near "
            f"{z['lat']}, {z['lon']}. Watching for ignition.",
            lat=z["lat"],
            lon=z["lon"],
            ignition_risk=z["ignition_risk"],
        )
        if config.settings.auto_alerts_enabled:
            await alerts.send_telegram(text)
    return zones
