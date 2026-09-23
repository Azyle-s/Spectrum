"""In-memory ring buffer of notable state changes (node connect/disconnect,
meshtastic reconnects, docker containers starting/stopping, tailscale peers
going up or down) -- purely for the LOG panel's "for fun" scrolling
journal. Nothing here is persisted: a deque that drops old entries is all
it needs, same as the CSI/history buffers elsewhere.
"""
import time
from collections import deque

_MAX_EVENTS = 50
_events: "deque[dict]" = deque(maxlen=_MAX_EVENTS)


def add(text: str) -> None:
    _events.append({"ts": time.time(), "text": text})


def snapshot() -> list[dict]:
    return list(reversed(_events))
