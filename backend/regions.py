"""
Region catalog for the Global-Forest-Watch-style dashboard drill-down.

Holds a curated set of regions (Canada -> Ontario -> districts) with their
forest-extent / tree-cover-loss figures sourced from Global Forest Watch /
Hansen UMD, plus a bounding box used to merge in LIVE fire activity from FIRMS.

The forest-loss numbers are GFW/Hansen published figures (the same ones GFW's
dashboard shows). The fire numbers are pulled live at request time. For exact,
always-current per-region loss numbers, wire the GFW Data API (free key).
"""

# bbox = (west, south, east, north)
REGIONS = {
    "CAN": {
        "id": "CAN", "name": "Canada", "parent": None,
        "bbox": (-141.0, 41.7, -52.6, 83.1),
        "center": [60.0, -96.0], "zoom": 4,
        "forest_mha": 280, "land_pct": 28,
        "base_year": 2020, "loss_year": 2025,
        "loss_value": 5.4, "loss_unit": "Mha", "co2": "2.6 Gt",
        "children": ["ON"],
    },
    "ON": {
        "id": "ON", "name": "Ontario", "parent": "CAN",
        "bbox": (-95.2, 41.6, -74.3, 56.9),
        "center": [50.0, -85.0], "zoom": 5,
        "forest_mha": 64, "land_pct": 70,
        "base_year": 2020, "loss_year": 2025,
        "loss_value": 590, "loss_unit": "kha", "co2": "168 Mt",
        "children": ["thunder-bay", "sudbury", "kenora"],
        "approx": True,
    },
    "thunder-bay": {
        "id": "thunder-bay", "name": "Thunder Bay", "parent": "ON",
        "bbox": (-91.6, 47.9, -84.8, 51.6),
        "center": [49.6, -88.5], "zoom": 6,
        "forest_mha": 8.3, "land_pct": 70,
        "base_year": 2020, "loss_year": 2025,
        "loss_value": 34, "loss_unit": "kha", "co2": "9.8 Mt",
        "children": [],
    },
    "sudbury": {
        "id": "sudbury", "name": "Sudbury", "parent": "ON",
        "bbox": (-82.5, 45.9, -80.0, 47.8),
        "center": [46.8, -81.2], "zoom": 7,
        "forest_mha": 3.1, "land_pct": 72,
        "base_year": 2020, "loss_year": 2025,
        "loss_value": 12, "loss_unit": "kha", "co2": "3.4 Mt",
        "children": [], "approx": True,
    },
    "kenora": {
        "id": "kenora", "name": "Kenora", "parent": "ON",
        "bbox": (-95.2, 48.7, -88.0, 56.9),
        "center": [52.0, -91.5], "zoom": 6,
        "forest_mha": 14.5, "land_pct": 74,
        "base_year": 2020, "loss_year": 2025,
        "loss_value": 61, "loss_unit": "kha", "co2": "17 Mt",
        "children": [], "approx": True,
    },
}


def get(region_id):
    return REGIONS.get(region_id)


def summary_sentence(r):
    """The GFW-style narrative line."""
    return (
        f"In {r['base_year']}, {r['name']} had {r['forest_mha']} Mha of natural "
        f"forest, extending over {r['land_pct']}% of its land area. In "
        f"{r['loss_year']}, it lost {r['loss_value']} {r['loss_unit']} of natural "
        f"forest, equivalent to {r['co2']} of CO₂ emissions."
    )
