"""
Protected areas — is this fire inside a park?

Point lookups against the World Database of Protected Areas (WDPA), served
free by UNEP-WCMC's ArcGIS server. A fire cluster inside a protected area is
a much stronger signal than one in a managed cutblock, so the alert pipeline
bumps severity when a new cluster lands in one.

If the remote service is down we fall back to a curated list of the big
Ontario parks so the demo never breaks.
"""
import logging

import httpx

log = logging.getLogger("forest-watch")

WDPA_BASE = (
    "https://data-gis.unep-wcmc.org/server/rest/services/ProtectedSites/"
    "The_World_Database_of_Protected_Areas/MapServer"
)
WDPA_QUERY = f"{WDPA_BASE}/1/query"
WDPA_EXPORT = f"{WDPA_BASE}/export"

# Fallback bboxes (w, s, e, n) for the marquee Ontario protected areas.
FALLBACK_PARKS = [
    ("Polar Bear Provincial Park", "Provincial Park", (-87.5, 54.4, -82.1, 56.0)),
    ("Woodland Caribou Provincial Park", "Provincial Park", (-95.2, 50.6, -94.2, 51.6)),
    ("Wabakimi Provincial Park", "Provincial Park", (-90.5, 50.3, -88.8, 51.1)),
    ("Quetico Provincial Park", "Provincial Park", (-92.2, 47.9, -90.8, 48.7)),
    ("Pukaskwa National Park", "National Park", (-86.3, 47.9, -85.5, 48.6)),
    ("Lake Superior Provincial Park", "Provincial Park", (-85.1, 47.2, -84.3, 47.9)),
    ("Algonquin Provincial Park", "Provincial Park", (-79.1, 45.2, -77.4, 46.1)),
    ("Killarney Provincial Park", "Provincial Park", (-81.8, 45.9, -81.0, 46.2)),
]

# Point lookups repeat heavily (clusters barely move hour to hour).
_cache = {}


async def lookup(lat: float, lon: float):
    """
    Return {"name", "designation", "iucn"} if (lat, lon) is inside a protected
    area, else None. Uses a tiny envelope intersect (the service's point
    queries are unreliable, envelopes work).
    """
    key = (round(lat, 2), round(lon, 2))
    if key in _cache:
        return _cache[key]

    pad = 0.02
    params = {
        "geometry": f"{lon - pad},{lat - pad},{lon + pad},{lat + pad}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "name,desig_eng,iucn_cat",
        "returnGeometry": "false",
        "f": "json",
    }
    hit = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(WDPA_QUERY, params=params)
        feats = resp.json().get("features") or []
        if feats:
            a = feats[0].get("attributes", {})
            hit = {
                "name": a.get("name"),
                "designation": a.get("desig_eng"),
                "iucn": a.get("iucn_cat"),
            }
    except Exception as exc:
        log.warning("WDPA lookup failed, trying curated fallback: %s", exc)

    # WDPA's polygon layer has gaps (Algonquin, notably). The curated list
    # backstops both service failures and missing polygons.
    if hit is None:
        for name, desig, (w, s, e, n) in FALLBACK_PARKS:
            if s <= lat <= n and w <= lon <= e:
                hit = {"name": name, "designation": desig, "iucn": None}
                break

    if len(_cache) > 4000:
        _cache.clear()
    _cache[key] = hit
    return hit


async def annotate_clusters(clusters):
    """
    Attach a `protected` field to each cluster (capped to keep remote calls
    light). Returns the list of protected-area hits for the alert message.
    """
    hits = []
    for c in clusters[:8]:
        p = await lookup(c["lat"], c["lon"])
        c["protected"] = p
        if p:
            hits.append({**p, "lat": c["lat"], "lon": c["lon"]})
    for c in clusters[8:]:
        c.setdefault("protected", None)
    return hits
