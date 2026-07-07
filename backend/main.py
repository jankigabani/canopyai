"""
Forest Watch — API server.

FastAPI app that powers the Ontario-focused dashboard:
  • serves the Leaflet frontend
  • proxies NASA FIRMS fire data (Ontario / Canada / custom bbox)
  • exposes near-real-time stats
  • runs the ML change-detection on demand
  • manages Telegram alert config + sends test alerts
  • runs an hourly background checker that alerts automatically

No database — data is fetched live from FIRMS and alert config lives in memory.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    ai, air, alerts, config, firms, gfw_api, impact, lightning, live,
    protected, regions, risk, scheduler, weather,
)

logging.basicConfig(level=logging.INFO)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Forest Watch API", version="0.3.0")

_lightning_task = None


@app.on_event("startup")
async def _startup():
    global _lightning_task
    scheduler.start_scheduler()
    _lightning_task = asyncio.create_task(lightning.run_client())


@app.on_event("shutdown")
async def _shutdown():
    if _lightning_task:
        _lightning_task.cancel()


# --- Health -----------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, **config.settings.as_dict()}


# --- Raw fire data (map points) ---------------------------------------------
@app.get("/api/fires")
async def fires(
    region: str = Query("ontario", description="ontario | canada | world | custom"),
    bbox: str | None = Query(None, description="Custom 'w,s,e,n' when region=custom"),
    days: int = Query(1, ge=1, le=5),
    source: str = Query("VIIRS_NOAA20_NRT"),
):
    area = _resolve_region(region, bbox)
    try:
        data = await firms.fetch_fires(area, days=days, source=source)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"count": len(data), "region": region, "days": days, "fires": data}


# --- Minutes-level detection (GOES) + live feed -------------------------------
def _fire_age_min(f):
    """Age in minutes from acq_date + acq_time (HHMM, UTC). None if unparsable."""
    try:
        t = int(f["acq_time"])
        dt = datetime.strptime(f["acq_date"], "%Y-%m-%d").replace(
            hour=t // 100, minute=t % 100, tzinfo=timezone.utc
        )
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 60))
    except (TypeError, ValueError, KeyError):
        return None


@app.get("/api/fires/latest")
async def fires_latest(minutes: int = Query(90, ge=10, le=1440)):
    """
    The freshest detections we have: GOES geostationary hotspots from the last
    N minutes. GOES rescans every ~10 minutes, so this is minutes-level, not
    the ~3 hour polar-orbit lag.
    """
    if not scheduler.GOES_LATEST["fetched_at"]:
        try:
            await scheduler.goes_poll()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
    out = []
    for f in scheduler.GOES_LATEST["fires"]:
        age = _fire_age_min(f)
        if age is not None and age <= minutes:
            out.append({**f, "age_min": age})
    out.sort(key=lambda f: f["age_min"])
    return {
        "count": len(out),
        "window_min": minutes,
        "fetched_at": scheduler.GOES_LATEST["fetched_at"],
        "fires": out,
    }


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    """Live event feed: GOES hotspots, lightning, ignition watches, alerts."""
    await live.manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # client pings; content ignored
    except WebSocketDisconnect:
        live.manager.drop(ws)


@app.get("/api/lightning")
async def lightning_now(minutes: int = Query(60, ge=5, le=180)):
    """Recent strikes over Canada + the current ignition-watch cells."""
    return {
        "available": lightning.STATE["connected"],
        "state": lightning.STATE,
        "strikes": lightning.recent_strikes(minutes)[:3000],
        "ignition": lightning.LAST_IGNITION["zones"],
        "checked_at": lightning.LAST_IGNITION["checked_at"],
    }


@app.get("/api/air")
async def air_quality():
    """Smoke picture for Ontario cities, worst first."""
    return {"cities": await air.city_smoke()}


# --- Dashboard stats --------------------------------------------------------
@app.get("/api/stats")
async def stats(days: int = Query(5, ge=1, le=5), source: str = "VIIRS_NOAA20_NRT"):
    """Near-real-time stats for the dashboard: Canada + Ontario + trend."""
    try:
        canada = await firms.fetch_fires(config.CANADA_BBOX, days=days, source=source)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    ontario = [f for f in canada if f["province"] == "Ontario"]
    today_on, yest_on = firms.split_today_yesterday(ontario)

    return {
        "days": days,
        "canada": firms.summarize(canada),
        "ontario": firms.summarize(ontario),
        "ontario_today": len(today_on),
        "ontario_yesterday": len(yest_on),
        "by_province": firms.by_province(canada),
        "ontario_trend": firms.daily_trend(ontario),
        "canada_trend": firms.daily_trend(canada),
    }


# --- ML change-detection ----------------------------------------------------
@app.post("/api/analysis/run")
async def run_analysis(send: bool = Query(False, description="Send alert if triggered")):
    """Run the day-over-day Ontario comparison now and return the result."""
    try:
        result = await scheduler.run_analysis(send_if_alert=send)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result


@app.get("/api/analysis/last")
def last_analysis():
    """The most recent analysis result (from the hourly job or a manual run)."""
    return scheduler.LAST_RESULT


# --- Region-change grid, risk forecast, impact, time-lapse, AI ---------------
async def _ontario_fires(days):
    try:
        return await firms.fetch_fires(config.ONTARIO_BBOX, days=days)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/grid")
async def grid(days: int = Query(5, ge=2, le=5)):
    """Region-based change cells (today vs yesterday) for the map overlay."""
    fires = await _ontario_fires(days)
    today, yesterday = firms.split_today_yesterday(fires)
    return {"cells": risk.change_grid(today, yesterday),
            "today_count": len(today), "yesterday_count": len(yesterday)}


@app.get("/api/risk")
async def risk_forecast(days: int = Query(5, ge=2, le=5)):
    """Predictive risk-forecast zones for tomorrow (density x fire weather)."""
    fires = await _ontario_fires(days)
    zones = await risk.risk_forecast(fires)
    return {"zones": zones}


@app.get("/api/impact")
async def impact_estimate(days: int = Query(5, ge=1, le=5)):
    """Estimated area burned, CO2, and trees-equivalent for Ontario."""
    fires = await _ontario_fires(days)
    today, _ = firms.split_today_yesterday(fires)
    return {"period": impact.estimate(fires), "today": impact.estimate(today),
            "days": days}


@app.get("/api/timelapse")
async def timelapse(days: int = Query(5, ge=2, le=5)):
    """Fires grouped by day, for the time-lapse animation."""
    fires = await _ontario_fires(days)
    by_day = {}
    for f in fires:
        by_day.setdefault(f["acq_date"], []).append(
            {"lat": f["lat"], "lon": f["lon"], "frp": f["frp"]}
        )
    frames = [{"date": d, "fires": by_day[d]} for d in sorted(by_day) if d]
    return {"frames": frames}


# Live context for the AI features, cached briefly so chat stays snappy.
_CTX_TTL = 120
_ctx_cache = {"at": 0.0, "ctx": None}


async def _live_context():
    now = time.monotonic()
    if _ctx_cache["ctx"] and now - _ctx_cache["at"] < _CTX_TTL:
        return _ctx_cache["ctx"]
    result = await scheduler.run_analysis(send_if_alert=False)
    fires = await _ontario_fires(5)
    today, _ = firms.split_today_yesterday(fires)
    zones = await risk.risk_forecast(fires)
    smoke = await air.city_smoke()
    goes_recent = [
        f for f in scheduler.GOES_LATEST["fires"]
        if (_fire_age_min(f) or 9999) <= 60
    ]
    ctx = {
        "analysis": {k: result[k] for k in (
            "today_count", "yesterday_count", "net_change", "pct_change",
            "new_cluster_count", "severity", "is_anomaly", "zscore")},
        "new_clusters": result["new_clusters"][:5],
        "protected_hits": result.get("protected_hits", []),
        "top_risk": zones[:5],
        "impact": impact.estimate(today),
        "goes_hotspots_last_hour": len(goes_recent),
        "smoke_worst_cities": smoke[:3],
        "lightning": {
            "feed_connected": lightning.STATE["connected"],
            "strikes_last_hour_canada": len(lightning.recent_strikes(60)),
            "ignition_watch_cells": lightning.LAST_IGNITION["zones"][:3],
        },
    }
    _ctx_cache.update(at=now, ctx=ctx)
    return ctx


@app.post("/api/briefing")
async def briefing():
    """Generate an AI situation briefing from the current data."""
    ctx = await _live_context()
    out = await ai.generate_briefing(ctx)
    return {**out, "context": ctx}


class AskBody(BaseModel):
    question: str


@app.post("/api/ask")
async def ask(body: AskBody):
    """Ask Canopy. Chat over the live forest data."""
    q = body.question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Empty question")
    ctx = await _live_context()
    return await ai.answer_question(q[:500], ctx)


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@app.get("/api/inspect")
async def inspect_point(lat: float = Query(...), lon: float = Query(...)):
    """Point intelligence. Click anywhere, get the live picture for that spot."""
    try:
        fires = await firms.fetch_fires(config.CANADA_BBOX, days=3)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    ranked = sorted(
        ((_haversine_km(lat, lon, f["lat"], f["lon"]), f) for f in fires),
        key=lambda t: t[0],
    )
    within_25 = sum(1 for d, _ in ranked if d <= 25)
    within_100 = sum(1 for d, _ in ranked if d <= 100)
    nearest = [
        {"km": round(d, 1), "lat": f["lat"], "lon": f["lon"],
         "frp": f["frp"], "date": f["acq_date"]}
        for d, f in ranked[:3]
    ]

    wx, smoke_list, prot = await asyncio.gather(
        weather.fire_weather_risk([(lat, lon)]),
        air.smoke_at([(lat, lon)]),
        protected.lookup(lat, lon),
    )
    w = wx[0] if wx else None
    smoke = smoke_list[0] if smoke_list else None

    if within_25:
        headline = f"This spot is hot. {within_25} fires within 25 km."
    elif within_100:
        headline = f"Nothing on top of this spot, but {within_100} fires within 100 km. Worth watching."
    else:
        headline = "Quiet right now. No fires within 100 km."
    if prot:
        headline += f" You are inside {prot['name']}."
    if w:
        headline += (
            f" Tomorrow looks {w['label'].lower()} risk here. "
            f"{w['temp_c']} C, wind {w['wind_kmh']} km/h, humidity {w['humidity_pct']} percent."
        )
    if smoke and smoke.get("pm25_now") is not None:
        headline += (
            f" Air quality is {smoke['label'].lower()}, "
            f"PM2.5 at {smoke['pm25_now']}."
        )

    return {
        "lat": lat, "lon": lon,
        "province": config.province_for(lat, lon),
        "fires_within_25km": within_25,
        "fires_within_100km": within_100,
        "nearest": nearest,
        "weather": w,
        "smoke": smoke,
        "protected": prot,
        "headline": headline,
    }


# --- GFW tile proxy (same-origin, so the browser can decode pixels) ---------
_TILE_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}


async def _proxy_tile(url):
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(url)
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="tile fetch failed")
    if r.status_code != 200:
        # Return a transparent 1x1 so the map degrades gracefully.
        return Response(status_code=204)
    return Response(content=r.content, media_type="image/png", headers=_TILE_CACHE_HEADERS)


@app.get("/api/tile/loss/{tcd}/{z}/{x}/{y}.png")
async def tile_loss(tcd: str, z: int, x: int, y: int):
    """Encoded tree-cover-loss tiles (year in blue, intensity in red)."""
    if tcd not in config.ALLOWED_TCD:
        raise HTTPException(status_code=400, detail="bad canopy threshold")
    url = f"{config.GFW_TILES}/umd_tree_cover_loss/{config.GFW_LOSS_VERSION}/{tcd}/{z}/{x}/{y}.png"
    return await _proxy_tile(url)


@app.get("/api/tile/glad/{z}/{x}/{y}.png")
async def tile_glad(z: int, x: int, y: int):
    """Encoded GLAD/RADD integrated deforestation-alert tiles (tropics)."""
    url = f"{config.GFW_TILES}/gfw_integrated_alerts/{config.GFW_ALERTS_VERSION}/default/{z}/{x}/{y}.png"
    return await _proxy_tile(url)


@app.get("/api/tile/dist/{z}/{x}/{y}.png")
async def tile_dist(z: int, x: int, y: int):
    """OPERA/UMD DIST-ALERT tiles — near-real-time vegetation disturbance,
    GLOBAL (including boreal Canada), every 2-4 days from HLS imagery."""
    url = f"{config.GFW_TILES}/umd_glad_dist_alerts/latest/default/{z}/{x}/{y}.png"
    return await _proxy_tile(url)


def _tile_bbox_3857(z, x, y):
    """Web-mercator (EPSG:3857) bbox in meters for a slippy tile."""
    n = 2 ** z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    r = 6378137.0
    mx = lambda lo: r * math.radians(lo)
    my = lambda la: r * math.log(math.tan(math.pi / 4 + math.radians(la) / 2))
    return mx(lon_w), my(lat_s), mx(lon_e), my(lat_n)


@app.get("/api/tile/protected/{z}/{x}/{y}.png")
async def tile_protected(z: int, x: int, y: int):
    """Protected-area polygons (WDPA), rendered by UNEP-WCMC's map server."""
    xmin, ymin, xmax, ymax = _tile_bbox_3857(z, x, y)
    url = (
        f"{protected.WDPA_EXPORT}?bbox={xmin},{ymin},{xmax},{ymax}"
        "&bboxSR=3857&imageSR=3857&size=256,256&format=png32"
        "&transparent=true&layers=show:1&f=image"
    )
    return await _proxy_tile(url)


# --- GFW-style region dashboard ---------------------------------------------
@app.get("/api/ontario/live")
async def ontario_live():
    """Live fire counts per Ontario district, from a single FIRMS fetch."""
    fires = await _ontario_fires(5)
    today, _ = firms.split_today_yesterday(fires)
    districts = []
    for cid in regions.REGIONS["ON"]["children"]:
        r = regions.REGIONS.get(cid)
        if not r:
            continue
        w, s, e, n = r["bbox"]

        def inside(f, w=w, s=s, e=e, n=n):
            return s <= f["lat"] <= n and w <= f["lon"] <= e

        districts.append({
            "id": cid, "name": r["name"],
            "today": sum(1 for f in today if inside(f)),
            "period": sum(1 for f in fires if inside(f)),
        })
    districts.sort(key=lambda d: d["today"], reverse=True)
    return {"total_today": len(today), "districts": districts}


@app.get("/api/regions")
def region_catalog():
    """Region tree for the breadcrumb / drill-down selector."""
    return {"regions": [
        {"id": r["id"], "name": r["name"], "parent": r["parent"],
         "children": r["children"], "center": r["center"], "zoom": r["zoom"]}
        for r in regions.REGIONS.values()]}


@app.get("/api/region/{region_id}")
async def region_detail(region_id: str):
    """Curated forest-loss stats (GFW/Hansen) + live fire activity for a region."""
    r = regions.get(region_id)
    if not r:
        raise HTTPException(status_code=404, detail="Unknown region")

    fires_today = fires_period = []
    fire_err = None
    try:
        all_fires = await firms.fetch_fires(tuple(r["bbox"]), days=5)
        fires_today, _ = firms.split_today_yesterday(all_fires)
        fires_period = all_fires
    except RuntimeError as exc:
        fire_err = str(exc)

    # Live tree-cover-loss by year from the GFW Data API (if a key is set).
    gfw_loss = await gfw_api.loss_by_year(tuple(r["bbox"]))

    return {
        "id": r["id"], "name": r["name"], "parent": r["parent"],
        "children": [{"id": regions.REGIONS[c]["name"], "value": c}
                     for c in r["children"] if c in regions.REGIONS],
        "center": r["center"], "zoom": r["zoom"], "bbox": r["bbox"],
        "approx": r.get("approx", False),
        "forest": {
            "forest_mha": r["forest_mha"], "land_pct": r["land_pct"],
            "base_year": r["base_year"], "loss_year": r["loss_year"],
            "loss_value": r["loss_value"], "loss_unit": r["loss_unit"],
            "co2": r["co2"], "source": "Global Forest Watch / Hansen UMD",
        },
        "summary": regions.summary_sentence(r),
        "loss_by_year": gfw_loss["by_year"] if gfw_loss else None,
        "loss_live": bool(gfw_loss),
        "fires": {
            "today": len(fires_today),
            "period": len(fires_period),
            "trend": firms.daily_trend(fires_period),
            "impact": impact.estimate(fires_today),
            "error": fire_err,
            "live": True,
        },
    }


# --- Alert config + Telegram ------------------------------------------------
class AlertConfig(BaseModel):
    telegram_chat_id: str | None = None
    new_cluster_threshold: int | None = None
    anomaly_zscore_threshold: float | None = None
    auto_alerts_enabled: bool | None = None


@app.get("/api/alert/config")
def get_alert_config():
    return config.settings.as_dict()


@app.post("/api/alert/config")
def set_alert_config(cfg: AlertConfig):
    s = config.settings
    if cfg.telegram_chat_id is not None:
        s.telegram_chat_id = cfg.telegram_chat_id.strip() or None
    if cfg.new_cluster_threshold is not None:
        s.new_cluster_threshold = max(1, cfg.new_cluster_threshold)
    if cfg.anomaly_zscore_threshold is not None:
        s.anomaly_zscore_threshold = cfg.anomaly_zscore_threshold
    if cfg.auto_alerts_enabled is not None:
        s.auto_alerts_enabled = cfg.auto_alerts_enabled
    return s.as_dict()


@app.get("/api/alert/chatid")
async def telegram_chat_ids():
    """Discover chat ids that have messaged the bot (helps the user set up)."""
    return await alerts.discover_chat_ids()


@app.post("/api/alert/test")
async def test_alert():
    """Send a test message to the configured Telegram chat."""
    status = await alerts.send_telegram(
        "<b>Canopy here. Test alert.</b>\n"
        "Your phone hookup works. You're all set."
    )
    if not status.get("ok"):
        raise HTTPException(status_code=400, detail=status.get("error"))
    return status


@app.get("/api/alert/log")
def alert_log():
    return {"alerts": config.ALERT_LOG}


# --- Helpers + frontend -----------------------------------------------------
def _resolve_region(region: str, bbox: str | None):
    region = region.lower()
    if region == "ontario":
        return config.ONTARIO_BBOX
    if region == "canada":
        return config.CANADA_BBOX
    if region == "world":
        return "world"
    if region == "custom" and bbox:
        return bbox
    raise HTTPException(status_code=400, detail="Invalid region/bbox")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        # no-cache so UI updates always reach the browser
        return FileResponse(
            FRONTEND_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )
