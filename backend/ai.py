"""
Canopy, the AI analyst. Powered by Groq.

Two jobs:
  1. generate_briefing(ctx)  -> short situation briefing from live data
  2. answer_question(q, ctx) -> conversational answers for the Ask Canopy chat

Voice: short sentences, plain words, no dashes, at most one emoji. If no
GROQ_API_KEY is configured (or the call fails) everything degrades to a
clean templated answer so the app never breaks.
"""
import json
import logging

from . import config

log = logging.getLogger("forest-watch")

MODEL = "llama-3.3-70b-versatile"

VOICE_RULES = (
    "Voice rules. Write like a sharp friend giving someone the rundown. "
    "Short sentences. Plain words. No dashes of any kind, use periods and commas instead. "
    "No markdown, no bullet points, no section headers. "
    "At most one emoji in the whole message, or none. "
    "Say the actual numbers. Never invent data that is not in the input. "
    "Example of the voice: '97 fires burning in Ontario right now. That is 13 more than "
    "yesterday. Four new clusters showed up overnight and the biggest is pushing 240 MW. "
    "If I were you I would watch the northwest corner tomorrow.'"
)

BRIEFING_SYSTEM = (
    "You are Canopy, the AI analyst inside CanopyAI, a live forest intelligence "
    "platform watching Canadian forests. You receive structured live data: fire "
    "detections, day over day change, new fire clusters, weather driven risk "
    "forecasts, and estimated carbon impact. Write a short situation briefing a "
    "forest ranger would actually want to read.\n\n"
    + VOICE_RULES +
    "\n\nShape. First line is the one sentence takeaway. Then a short paragraph on "
    "what is happening, grounded in the numbers. Then one or two lines on where "
    "risk is heading next, using the forecast data. Close with the single most "
    "useful action, one line. Keep the whole thing under 150 words."
)

CHAT_SYSTEM = (
    "You are Canopy, the AI analyst inside CanopyAI, a live forest intelligence "
    "platform for Canada. The user is looking at a live map and asking you "
    "questions. You get the current live data as JSON: Ontario fire analysis, new "
    "fire clusters, tomorrow's risk zones, and estimated carbon impact.\n\n"
    + VOICE_RULES +
    "\n\nAnswer the user's question directly using the data. If the question is "
    "about something the data does not cover, say so honestly and tell them what "
    "you can see instead. You can also explain the map layers if asked: tree cover "
    "loss is annual data through 2024 from Hansen UMD, DIST-ALERT is near real "
    "time vegetation disturbance updated every few days and covers Canada, GLAD "
    "RADD alerts cover tropical forests only, and fires come from NASA FIRMS with "
    "about a three hour delay. Keep answers under 120 words."
)


def _fallback_briefing(ctx):
    a = ctx.get("analysis", {})
    imp = ctx.get("impact", {})
    risk = ctx.get("top_risk", [])
    lines = [
        f"{a.get('today_count', 0)} fires burning in Ontario right now.",
    ]
    net = a.get("net_change", 0)
    if net > 0:
        lines.append(f"That is {net} more than yesterday.")
    elif net < 0:
        lines.append(f"That is {abs(net)} fewer than yesterday.")
    if a.get("new_cluster_count"):
        lines.append(f"We found {a['new_cluster_count']} new fire clusters that were not there yesterday.")
    lines.append(
        f"We estimate about {imp.get('area_km2', 0)} km2 burned today, "
        f"roughly {imp.get('co2_kilotonnes', 0)} kt of CO2."
    )
    if risk:
        t = risk[0]
        lines.append(
            f"Tomorrow the highest risk sits near ({t['lat']}, {t['lon']}) "
            f"at {t['forecast_risk']} out of 100."
        )
    lines.append("Connect a Groq key and I will write these myself.")
    return "\n".join(lines)


def _fallback_chat(question, ctx):
    a = ctx.get("analysis", {})
    return (
        "My AI brain is not connected yet, so here is the raw picture. "
        f"{a.get('today_count', 0)} fires in Ontario today, "
        f"{a.get('new_cluster_count', 0)} new clusters, severity {a.get('severity', 'low')}. "
        "Add a GROQ_API_KEY to .env and I can actually answer questions."
    )


async def _call_groq(system, user_text, max_tokens=1200):
    from groq import AsyncGroq

    client = AsyncGroq(api_key=config.GROQ_API_KEY)
    msg = await client.chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    )
    return (msg.choices[0].message.content or "").strip()


async def generate_briefing(ctx: dict) -> dict:
    """Returns {"text": briefing, "ai": bool}."""
    if not config.GROQ_API_KEY:
        return {"text": _fallback_briefing(ctx), "ai": False}
    try:
        text = await _call_groq(
            BRIEFING_SYSTEM,
            "Here is the current Ontario forest data as JSON. Write the briefing.\n\n"
            + json.dumps(ctx, default=str),
        )
        return {"text": text or _fallback_briefing(ctx), "ai": True}
    except Exception as exc:
        log.warning("AI briefing failed: %s", exc)
        return {"text": _fallback_briefing(ctx), "ai": False}


async def answer_question(question: str, ctx: dict) -> dict:
    """Ask Canopy chat. Returns {"text": answer, "ai": bool}."""
    if not config.GROQ_API_KEY:
        return {"text": _fallback_chat(question, ctx), "ai": False}
    try:
        text = await _call_groq(
            CHAT_SYSTEM,
            "Live data right now:\n" + json.dumps(ctx, default=str)
            + "\n\nUser question: " + question.strip(),
            max_tokens=800,
        )
        return {"text": text or _fallback_chat(question, ctx), "ai": True}
    except Exception as exc:
        log.warning("AI chat failed: %s", exc)
        return {"text": _fallback_chat(question, ctx), "ai": False}
