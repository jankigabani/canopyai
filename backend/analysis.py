"""
Change-detection / ML module — the core of the alert pipeline.

Given *today's* and *yesterday's* Ontario fire detections (plus a short history
of daily counts), it:

  1. Clusters today's and yesterday's fires spatially with DBSCAN.
  2. Flags clusters that are NEW today (no nearby cluster existed yesterday)
     — these are likely newly-started / spreading fire events.
  3. Runs a statistical anomaly check: is today's total unusually high vs the
     recent baseline? (z-score over the trailing daily counts).
  4. Scores an overall severity and decides whether to alert.

It's deliberately transparent and dependency-light (scikit-learn DBSCAN +
numpy). The interface — `detect_change(...)` returning a result dict — is the
seam where you can later drop in a heavier trained model (e.g. NDVI-based
deforestation) without changing the scheduler, API, or frontend.
"""
import numpy as np
from sklearn.cluster import DBSCAN

# DBSCAN tuning. eps is in degrees: ~0.12 deg ≈ 13 km, so detections within
# roughly that distance group into one "fire event". min_samples avoids
# treating a single lonely pixel as a cluster.
_EPS_DEG = 0.12
_MIN_SAMPLES = 3
# Two cluster centroids closer than this are treated as "the same event".
_SAME_EVENT_DEG = 0.2


def _cluster(fires):
    """Return a list of cluster dicts: centroid, size, total/avg FRP."""
    if len(fires) < _MIN_SAMPLES:
        return []

    coords = np.array([[f["lat"], f["lon"]] for f in fires])
    labels = DBSCAN(eps=_EPS_DEG, min_samples=_MIN_SAMPLES).fit_predict(coords)

    clusters = []
    for label in set(labels):
        if label == -1:  # noise / unclustered points
            continue
        members = [fires[i] for i in range(len(fires)) if labels[i] == label]
        lats = [m["lat"] for m in members]
        lons = [m["lon"] for m in members]
        frps = [m["frp"] for m in members if m["frp"] is not None]
        clusters.append(
            {
                "lat": round(sum(lats) / len(lats), 4),
                "lon": round(sum(lons) / len(lons), 4),
                "size": len(members),
                "total_frp": round(sum(frps), 1) if frps else 0.0,
                "max_frp": round(max(frps), 1) if frps else 0.0,
            }
        )
    return clusters


def _is_new(cluster, prior_clusters):
    """True if no prior cluster sits within _SAME_EVENT_DEG of this one."""
    for p in prior_clusters:
        d = np.hypot(cluster["lat"] - p["lat"], cluster["lon"] - p["lon"])
        if d <= _SAME_EVENT_DEG:
            return False
    return True


def _zscore(value, history):
    """z-score of value vs a history list. 0 if not enough history."""
    arr = np.array(history, dtype=float)
    if arr.size < 3 or arr.std() == 0:
        return 0.0
    return round(float((value - arr.mean()) / arr.std()), 2)


def detect_change(today_fires, yesterday_fires, history_counts, settings):
    """
    Run the full comparison and return a result dict including `should_alert`
    and a ready-to-send `message`.

    history_counts: list of trailing daily Ontario fire totals (e.g. last 7
    days) used as the anomaly baseline.
    """
    today_clusters = _cluster(today_fires)
    yesterday_clusters = _cluster(yesterday_fires)

    new_clusters = [c for c in today_clusters if _is_new(c, yesterday_clusters)]
    new_clusters.sort(key=lambda c: c["total_frp"], reverse=True)

    today_count = len(today_fires)
    yest_count = len(yesterday_fires)
    net_change = today_count - yest_count
    pct_change = round((net_change / yest_count * 100), 1) if yest_count else None

    zscore = _zscore(today_count, history_counts)
    is_anomaly = zscore >= settings.anomaly_zscore_threshold

    # Severity: escalate on number of new events, their intensity, and anomaly.
    hottest = new_clusters[0]["total_frp"] if new_clusters else 0.0
    if len(new_clusters) >= 3 or hottest >= 100 or zscore >= 3:
        severity = "high"
    elif new_clusters or is_anomaly:
        severity = "medium"
    else:
        severity = "low"

    should_alert = (
        len(new_clusters) >= settings.new_cluster_threshold or is_anomaly
    )

    result = {
        "today_count": today_count,
        "yesterday_count": yest_count,
        "net_change": net_change,
        "pct_change": pct_change,
        "zscore": zscore,
        "is_anomaly": is_anomaly,
        "today_clusters": len(today_clusters),
        "new_clusters": new_clusters,
        "new_cluster_count": len(new_clusters),
        "severity": severity,
        "should_alert": should_alert,
    }
    result["message"] = build_message(result)
    return result


def build_message(r):
    """Alert text for Telegram. Canopy voice: short lines, plain words, no dashes."""
    lines = [
        f"🔥 <b>Canopy alert. Severity {r['severity'].upper()}.</b>",
        "",
        f"{r['today_count']} fires burning in Ontario right now.",
    ]
    net = r["net_change"]
    if net > 0:
        lines.append(f"That is {net} more than yesterday.")
    elif net < 0:
        lines.append(f"That is {abs(net)} fewer than yesterday.")
    else:
        lines.append("Same count as yesterday.")

    n = r["new_cluster_count"]
    if n:
        word = "cluster" if n == 1 else "clusters"
        lines.append(f"We found {n} new fire {word} that did not exist yesterday.")
        c = r["new_clusters"][0]
        lines.append(
            f"The biggest sits near ({c['lat']}, {c['lon']}) with {c['size']} "
            f"detections pushing {c['total_frp']} MW."
        )
    if r["is_anomaly"]:
        lines.append(
            f"Today is unusual. The fire count is {r['zscore']} standard "
            "deviations above the weekly norm."
        )
    lines.append("")
    lines.append("CanopyAI. Watching the forest so you don't have to.")
    return "\n".join(lines)
