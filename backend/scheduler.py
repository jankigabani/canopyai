"""
The automated heartbeat: fetch Ontario data, run change-detection, alert.

`run_analysis` is the shared pipeline used by BOTH the hourly background job
and the manual "Run check now" API endpoint. The APScheduler job simply calls
it once an hour with alerting enabled.
"""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import ai, alerts, analysis, config, firms, impact, risk

log = logging.getLogger("forest-watch")

# Cache of the most recent analysis so the dashboard can show it instantly.
LAST_RESULT = {"result": None, "ran_at": None}

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

    alert_status = None
    if send_if_alert and result["should_alert"] and config.settings.auto_alerts_enabled:
        message = result["message"]
        imp = result["impact"]
        message += (
            f"\n\n🌍 Est. impact today: ~{imp['area_km2']} km² · "
            f"~{imp['co2_kilotonnes']} kt CO₂"
        )
        # If AI is configured, attach a short analyst briefing to the alert.
        if config.ANTHROPIC_API_KEY:
            try:
                zones = await risk.risk_forecast(fires)
                brief = await ai.generate_briefing({
                    "analysis": {k: result[k] for k in (
                        "today_count", "yesterday_count", "net_change",
                        "new_cluster_count", "severity", "is_anomaly")},
                    "new_clusters": result["new_clusters"][:5],
                    "top_risk": zones[:5],
                    "impact": imp,
                })
                if brief.get("ai"):
                    message += "\n\n🤖 <b>AI briefing</b>\n" + brief["text"]
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


def start_scheduler():
    """Start the hourly background job (no-op if no FIRMS key configured)."""
    global _scheduler
    if not config.FIRMS_MAP_KEY:
        log.warning("FIRMS_MAP_KEY not set — scheduler not started.")
        return
    if _scheduler:
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(_hourly_job, "interval", hours=1, id="ontario_check")
    _scheduler.start()
    log.info("Scheduler started — Ontario check runs every hour.")
