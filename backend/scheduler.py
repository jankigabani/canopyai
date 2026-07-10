"""
The automated heartbeat: fetch Ontario data, run change-detection, alert.

`run_analysis` is the shared pipeline used by BOTH the hourly background job
and the manual "Run check now" API endpoint. On top of that sit two fast
jobs: a GOES watcher (every 2 minutes, minutes-level fire detection) and a
lightning ignition check (every 5 minutes). Both push to the live WebSocket
feed.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import (
    ai, alerts, analysis, config, firms, gfw_alerts, impact, lightning, live,
    protected, risk,
)

log = logging.getLogger("forest-watch")

# Cache of the most recent analysis so the dashboard can show it instantly.
LAST_RESULT = {"result": None, "ran_at": None}

# Latest GOES snapshot (Canada, today). Refreshed by the 2-minute watcher.
GOES_LATEST = {"fires": [], "fetched_at": None}
_goes_seen = set()
_goes_primed = False

_scheduler = None


async def run_analysis(send_if_alert: bool = True) -> dict:
    """
    Fetch the last 7 days of Ontario fires, split today vs yesterday, run the
    ML change-detection, and (optionally) send a Telegram alert if the
    thresholds are crossed. Returns the analysis result dict.
    """
    fires = await firms.fetch_fires(config.ONTARIO_BBOX, days=5)
    today, yesterday = firms.split_today_yesterday(fires)

    # Baseline = daily counts for the days BEFORE today.
    trend = firms.daily_trend(fires)
    history_counts = [d["count"] for d in trend[:-1]] if len(trend) > 1 else []

    result = analysis.detect_change(today, yesterday, history_counts, config.settings)
    result["impact"] = impact.estimate(today)

    # A new cluster inside a protected area is the worst kind of news:
    # escalate severity and say so in the message.
    protected_hits = await protected.annotate_clusters(result["new_clusters"])
    result["protected_hits"] = protected_hits
    if protected_hits:
        result["severity"] = "high"
        result["message"] = analysis.build_message(result)

    if result["should_alert"]:
        await live.emit(
            "alert",
            f"{result['today_count']} fires in Ontario, severity "
            f"{result['severity']}, {result['new_cluster_count']} new clusters."
            + (
                f" One is inside {protected_hits[0]['name']}."
                if protected_hits
                else ""
            ),
            severity=result["severity"],
        )

    alert_status = None
    if send_if_alert and result["should_alert"] and config.settings.auto_alerts_enabled:
        message = result["message"]
        imp = result["impact"]
        message += (
            f"\n\nWe estimate about {imp['area_km2']} km2 burned today. "
            f"Roughly {imp['co2_kilotonnes']} kt of CO2."
        )
        # If AI is configured, attach a short analyst briefing to the alert.
        if config.GROQ_API_KEY:
            try:
                zones = await risk.risk_forecast(fires)
                brief = await ai.generate_briefing({
                    "analysis": {k: result[k] for k in (
                        "today_count", "yesterday_count", "net_change",
                        "new_cluster_count", "severity", "is_anomaly")},
                    "new_clusters": result["new_clusters"][:5],
                    "protected_hits": protected_hits,
                    "top_risk": zones[:5],
                    "impact": imp,
                })
                if brief.get("ai"):
                    message += "\n\n<b>Canopy's take</b>\n" + brief["text"]
            except Exception as exc:
                log.warning("AI briefing failed: %s", exc)
        alert_status = await alerts.send_telegram(message)
        config.record_alert(
            {
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "severity": result["severity"],
                "new_clusters": result["new_cluster_count"],
                "today_count": result["today_count"],
                "delivered": alert_status.get("ok", False),
                "error": alert_status.get("error"),
            }
        )

    LAST_RESULT["result"] = result
    LAST_RESULT["ran_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["alert_delivery"] = alert_status
    return result


async def _hourly_job():
    try:
        result = await run_analysis(send_if_alert=True)
        log.info(
            "Hourly check: today=%s new_clusters=%s alert=%s",
            result["today_count"],
            result["new_cluster_count"],
            result["should_alert"],
        )
    except Exception as exc:  # never let a bad run kill the scheduler
        log.warning("Hourly check failed: %s", exc)


async def goes_poll():
    """
    Fetch today's GOES detections for Canada (~10 minute satellite refresh)
    and broadcast anything we haven't seen before. First run just primes the
    seen-set so a server restart doesn't replay the whole day as "new".
    """
    global _goes_primed
    fires = await firms.fetch_fires(config.CANADA_BBOX, days=1, source="GOES_NRT")
    GOES_LATEST["fires"] = fires
    GOES_LATEST["fetched_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    new = []
    for f in fires:
        key = (f["lat"], f["lon"], f["acq_date"], f["acq_time"])
        if key not in _goes_seen:
            _goes_seen.add(key)
            new.append(f)
    if len(_goes_seen) > 200_000:
        _goes_seen.clear()
        _goes_seen.update(
            (f["lat"], f["lon"], f["acq_date"], f["acq_time"]) for f in fires
        )

    if not _goes_primed:
        _goes_primed = True
        return new

    for f in new[:8]:
        where = f", {f['province']}" if f.get("province") else ""
        frp = f" pushing {round(f['frp'])} MW" if f.get("frp") else ""
        await live.emit(
            "fire",
            f"GOES satellite sees a hotspot near {f['lat']:.2f}, {f['lon']:.2f}"
            f"{where}{frp}. Minutes old.",
            lat=f["lat"],
            lon=f["lon"],
            frp=f.get("frp"),
            province=f.get("province"),
        )
    if len(new) > 8:
        await live.emit(
            "fire",
            f"{len(new)} fresh GOES hotspots across Canada this pass.",
            count=len(new),
        )
    return new


async def _goes_job():
    try:
        await goes_poll()
    except Exception as exc:
        log.warning("GOES poll failed: %s", exc)


async def _ignition_job():
    try:
        await lightning.check_and_alert()
    except Exception as exc:
        log.warning("Ignition check failed: %s", exc)


# Previous DIST fetch, persisted so restarts don't lose the comparison base.
_DIST_STATE_FILE = Path(__file__).resolve().parent.parent / ".dist_state.json"
# Ignore reprocessing wiggles below this; a real new pass adds thousands of ha.
_DIST_DELTA_MIN_HA = 250


def _load_dist_state():
    try:
        return json.loads(_DIST_STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


async def _dist_alert_job():
    """
    Refresh the current-year disturbance stats for Ontario and compare with
    the previous fetch. When a new satellite pass lands, tell the phone how
    much fresh disturbance it brought.
    """
    try:
        data = await gfw_alerts.alerts_this_year(config.ONTARIO_BBOX, force=True)
    except Exception as exc:
        log.warning("DIST-ALERT refresh failed: %s", exc)
        return
    if not data:
        return
    log.info(
        "DIST-ALERT %s: %s ha YTD in Ontario, latest %s",
        data["year"], data["total_ha"], data["latest_date"],
    )

    prev = _load_dist_state()
    try:
        _DIST_STATE_FILE.write_text(json.dumps({
            "year": data["year"],
            "total_ha": data["total_ha"],
            "latest_date": data["latest_date"],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }))
    except OSError as exc:
        log.warning("Could not persist DIST state: %s", exc)

    if not prev or prev.get("year") != data["year"]:
        return  # first fetch (or new year): nothing to compare against yet

    delta = data["total_ha"] - prev.get("total_ha", 0)
    new_pass = data["latest_date"] != prev.get("latest_date")
    if delta < _DIST_DELTA_MIN_HA and not (new_pass and delta > 0):
        return

    await live.emit(
        "alert",
        f"New satellite pass. {delta:,} ha of fresh disturbance detected in "
        f"Ontario since the last check. {data['year']} total is now "
        f"{data['total_ha']:,} ha.",
        severity="medium",
    )
    if config.settings.auto_alerts_enabled:
        wow = data.get("wow_pct")
        wow_line = (
            f"That is {'up' if wow > 0 else 'down'} {abs(wow)} percent on the week before.\n"
            if wow is not None else ""
        )
        await alerts.send_telegram(
            "🛰 <b>Canopy satellite update.</b>\n\n"
            f"Fresh vegetation disturbance in Ontario: {delta:,} ha since the last check.\n"
            f"That brings {data['year']} to {data['total_ha']:,} ha.\n"
            f"Last 7 days: {data['last7_ha']:,} ha.\n"
            + wow_line +
            f"Latest detection {data['latest_date']}.\n\n"
            "CanopyAI. Watching the forest so you don't have to."
        )


def start_scheduler():
    """Start the background jobs (no-op if no FIRMS key configured)."""
    global _scheduler
    if not config.FIRMS_MAP_KEY:
        log.warning("FIRMS_MAP_KEY not set — scheduler not started.")
        return
    if _scheduler:
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(_hourly_job, "interval", hours=1, id="ontario_check")
    _scheduler.add_job(_goes_job, "interval", minutes=2, id="goes_watch")
    _scheduler.add_job(_ignition_job, "interval", minutes=5, id="ignition_watch")
    _scheduler.add_job(
        _dist_alert_job, "interval", hours=6, id="dist_alert_prefetch",
        next_run_time=datetime.now(timezone.utc),
    )
    _scheduler.start()
    log.info(
        "Scheduler started — hourly Ontario check, GOES watch every 2 min, "
        "ignition check every 5 min."
    )
