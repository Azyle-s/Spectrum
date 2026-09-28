"""Background WebSocket client -- connects to the same /ws endpoint the
web dashboard uses, keeps the latest payload in a shared dict behind a
lock. Runs its own asyncio loop in a daemon thread since pygame's main
loop is synchronous.
"""
import asyncio
import json
import os
import threading

import websockets

WS_URL = os.environ.get("SPECTRUM_WS_URL", "ws://localhost:8000/ws")

_state: dict = {}
_lock = threading.Lock()
_connected = False


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def is_connected() -> bool:
    return _connected


def _run():
    global _connected

    async def loop():
        global _connected
        while True:
            try:
                async with websockets.connect(WS_URL) as ws:
                    _connected = True
                    async for message in ws:
                        payload = json.loads(message)
                        with _lock:
                            _state.update(payload)
            except Exception:
                _connected = False
                await asyncio.sleep(1.0)

    asyncio.run(loop())


def start() -> None:
    threading.Thread(target=_run, daemon=True, name="ws-client").start()
