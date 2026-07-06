"""
AI situation briefings — powered by Claude.

Takes the structured analysis (change detection, weather risk, impact) and asks
Claude to write a short, natural-language forest-intelligence briefing with
recommended actions — the kind of thing an analyst would hand a ranger.

Uses the official Anthropic SDK (claude-opus-4-8, adaptive thinking). If no
ANTHROPIC_API_KEY is configured, it degrades gracefully to a templated summary
so the rest of the app keeps working.
"""
import json

from . import config

MODEL = "claude-opus-4-8"

SYSTEM = (
    "You are CanopyAI, a forest-fire intelligence analyst monitoring Ontario, "
    "Canada. You receive structured near-real-time data (fire detections, "
    "day-over-day change, new fire clusters, fire-weather risk forecasts, and "
    "estimated carbon impact). Write a concise situation briefing for a forest "
    "ranger or emergency manager.\n\n"
    "Format with short sections:\n"
    "1. HEADLINE — one sentence on the overall situation.\n"
    "2. WHAT'S HAPPENING — 2-4 bullet points grounded in the numbers.\n"
    "3. WATCH ZONES — where risk is rising next (use the forecast data).\n"
    "4. RECOMMENDED ACTIONS — 2-3 concrete, prioritized actions.\n\n"
    "Be specific and cite the numbers. Do not invent data not present. Keep it "
    "under ~200 words. Plain text, no markdown headers beyond the section labels."
)


def _fallback(ctx):
    """Templated briefing when Claude isn't configured."""
    a = ctx.get("analysis", {})
    risk = ctx.get("top_risk", [])
    impact = ctx.get("impact", {})
    lines = [
        f"HEADLINE: {a.get('today_count', 0)} active fire detections in Ontario "
        f"today ({a.get('net_change', 0):+d} vs yesterday); "
        f"{a.get('new_cluster_count', 0)} new cluster(s).",
        "",
        "WHAT'S HAPPENING:",
        f"- Severity assessed as {a.get('severity', 'low').upper()}.",
        f"- Estimated impact so far: ~{impact.get('area_km2', 0)} km² burned, "
        f"~{impact.get('co2_kilotonnes', 0)} kt CO2.",
    ]
    if risk:
        top = risk[0]
        lines += ["", "WATCH ZONES:",
                  f"- Highest forecast risk near ({top['lat']}, {top['lon']}) "
                  f"(risk {top['forecast_risk']}/100)."]
    lines += ["", "RECOMMENDED ACTIONS:",
              "- Monitor the new clusters above.",
              "- Pre-position resources near the highest forecast-risk zones.",
              "", "(Set ANTHROPIC_API_KEY for AI-written briefings.)"]
    return "\n".join(lines)


async def generate_briefing(ctx: dict) -> dict:
    """
    ctx: {analysis, top_risk, impact, weather_summary?}.
    Returns {"text": briefing, "ai": bool}.
    """
    if not config.ANTHROPIC_API_KEY:
        return {"text": _fallback(ctx), "ai": False}

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return {"text": _fallback(ctx), "ai": False}

    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    user = (
        "Here is the current Ontario forest-monitoring data as JSON. Write the "
        "briefing.\n\n" + json.dumps(ctx, default=str)
    )
    try:
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=1200,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        return {"text": text or _fallback(ctx), "ai": True}
    except Exception as exc:  # network/auth/etc — never break the dashboard
        return {"text": _fallback(ctx) + f"\n\n(AI error: {exc})", "ai": False}
