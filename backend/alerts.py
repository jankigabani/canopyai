"""
Telegram alert delivery.

Sends alert messages to a Telegram chat using the Bot API. Also exposes a
helper to discover your chat id (so the user doesn't have to look it up by
hand): after they message their bot, getUpdates returns the chat id.
"""
import httpx

from . import config

TG_API = "https://api.telegram.org/bot{token}/{method}"


async def send_telegram(text: str, chat_id: str | None = None) -> dict:
    """Send `text` to the configured (or given) chat. Returns a status dict."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.settings.telegram_chat_id

    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set in .env"}
    if not chat_id:
        return {"ok": False, "error": "No Telegram chat id configured yet"}

    url = TG_API.format(token=token, method="sendMessage")
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description", "Telegram error")}
        return {"ok": True}
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"Could not reach Telegram: {exc}"}


async def discover_chat_ids() -> dict:
    """
    Call getUpdates and pull out any chat ids that have messaged the bot.
    The user sends '/start' (or any message) to their bot, then we can read
    their chat id here and save it — no manual lookup needed.
    """
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set in .env"}

    url = TG_API.format(token=token, method="getUpdates")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
        data = resp.json()
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"Could not reach Telegram: {exc}"}

    chats = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            chats[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
    return {
        "ok": True,
        "chats": [{"chat_id": cid, "name": name} for cid, name in chats.items()],
    }
