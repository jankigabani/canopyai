"""
Live event bus — the real-time heartbeat of the map.

A tiny WebSocket fan-out: backend jobs push events (new GOES hotspots,
lightning strikes, ignition watches, alerts) and every connected browser
gets them instantly. A small ring buffer of recent events is sent on
connect so the feed isn't empty when the page loads.
"""
import json
import logging
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("forest-watch")

# Newest first. Sent to each client on connect as a "hello" event.
RECENT = deque(maxlen=60)


class _Manager:
    def __init__(self):
        self.sockets = set()

    async def connect(self, ws):
        await ws.accept()
        self.sockets.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "hello", "events": list(RECENT)[:12]}))
        except Exception:
            self.drop(ws)

    def drop(self, ws):
        self.sockets.discard(ws)

    async def broadcast(self, event: dict):
        event.setdefault(
            "at", datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        RECENT.appendleft(event)
        payload = json.dumps(event)
        for ws in list(self.sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                self.drop(ws)


manager = _Manager()


async def emit(type_: str, text: str, **extra):
    """Push one event to every connected client (and the recent buffer)."""
    await manager.broadcast({"type": type_, "text": text, **extra})
