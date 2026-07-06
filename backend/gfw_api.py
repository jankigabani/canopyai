"""
Global Forest Watch Data API — live tree-cover-loss statistics.

If a GFW_API_KEY is configured, this queries the GFW Data API for real
tree-cover-loss area (by year) inside a region's bounding box, so the dashboard
can show live numbers instead of curated ones. Without a key (or on any error)
callers fall back to the curated figures in regions.py.

Get a free key: https://www.globalforestwatch.org/help/developers/guides/create-and-use-an-api-key/
"""
import httpx

from . import config

DATASET = "umd_tree_cover_loss"
VERSION = "latest"
QUERY_URL = f"https://data-api.globalforestwatch.org/dataset/{DATASET}/{VERSION}/query/json"


def _bbox_polygon(bbox):
    w, s, e, n = bbox
    return {
        "type": "Polygon",
        "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
    }


async def loss_by_year(bbox, canopy=30):
    """
    Return {"by_year": [{"year", "area_ha"}], "total_ha", "live": True} for the
    bbox, or None if not configured / query failed.
    """
    if not config.GFW_API_KEY:
        return None

    sql = (
        "SELECT umd_tree_cover_loss__year AS year, SUM(area__ha) AS area "
        "FROM results "
        f"WHERE umd_tree_cover_loss__year >= 2001 "
        f"AND umd_tree_cover_density_2000__threshold >= {canopy} "
        "GROUP BY umd_tree_cover_loss__year "
        "ORDER BY umd_tree_cover_loss__year"
    )
    payload = {"sql": sql, "geometry": _bbox_polygon(bbox)}
    headers = {"x-api-key": config.GFW_API_KEY, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.post(QUERY_URL, json=payload, headers=headers)
        if resp.status_code != 200:
            return None
        rows = resp.json().get("data", [])
    except (httpx.RequestError, ValueError):
        return None

    by_year, total = [], 0.0
    for row in rows:
        area = round(row.get("area") or 0.0)
        yr = row.get("year")
        if yr is None:
            continue
        by_year.append({"year": int(yr), "area_ha": area})
        total += area
    if not by_year:
        return None
    return {"by_year": by_year, "total_ha": round(total), "live": True}
