"""
Carbon & impact estimator.

Turns raw fire detections into tangible impact numbers — estimated area burned,
CO2 emitted, and a "trees-equivalent" figure — for the dashboard and the pitch.

These are deliberately transparent first-order estimates, not a research-grade
emissions model. Constants are labelled so they're easy to defend/tune. Every
number is an ESTIMATE and the UI says so.
"""

# --- Constants (rough, boreal-forest oriented) ------------------------------
# A VIIRS detection is a ~375 m pixel ≈ 14 ha. Detections overlap and not all of
# a pixel burns, so we scale down with an effective-fraction factor.
HA_PER_DETECTION = 14.0
EFFECTIVE_FRACTION = 0.4

# Aboveground biomass and combustion (boreal forest, order-of-magnitude).
BIOMASS_T_PER_HA = 100.0      # tonnes of biomass per hectare
COMBUSTION_COMPLETENESS = 0.3  # fraction actually combusted in a fire
CARBON_FRACTION = 0.47         # carbon content of biomass
CO2_PER_CARBON = 3.67          # molecular mass ratio C -> CO2

# One mature tree absorbs ~21 kg CO2 / year -> "trees-equivalent for a year".
CO2_KG_PER_TREE_YEAR = 21.0


def estimate(fires):
    """Return an impact estimate dict for a list of fire detections."""
    n = len(fires)
    area_ha = n * HA_PER_DETECTION * EFFECTIVE_FRACTION

    biomass_burned_t = area_ha * BIOMASS_T_PER_HA * COMBUSTION_COMPLETENESS
    carbon_t = biomass_burned_t * CARBON_FRACTION
    co2_t = carbon_t * CO2_PER_CARBON
    trees_equiv = (co2_t * 1000) / CO2_KG_PER_TREE_YEAR

    return {
        "detections": n,
        "area_ha": round(area_ha),
        "area_km2": round(area_ha / 100, 1),
        "co2_tonnes": round(co2_t),
        "co2_kilotonnes": round(co2_t / 1000, 1),
        "trees_equivalent": round(trees_equiv),
        "is_estimate": True,
    }
