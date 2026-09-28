"""Second feasibility spike: same visual (CPU ring + node-driven wave) as
spike.py, but using pygame's low-level SDL2 Renderer API
(pygame._sdl2.video), which is actually hardware-accelerated -- unlike
the classic pygame.draw.* primitives spike.py used, which are CPU-side
software rasterization. If this isn't dramatically cheaper than spike.py,
the native-app avenue isn't worth pursuing further.
"""
import asyncio
import json
import math
import os
import threading

import pygame
from pygame._sdl2.video import Renderer, Texture, Window
import websockets

WS_URL = os.environ.get("SPECTRUM_WS_URL", "ws://localhost:8000/ws")

state = {"system": {}, "nodes": []}
state_lock = threading.Lock()


def ws_thread():
    async def run():
        while True:
            try:
                async with websockets.connect(WS_URL) as ws:
                    async for message in ws:
                        payload = json.loads(message)
                        with state_lock:
                            state["system"] = payload.get("system") or {}
                            state["nodes"] = payload.get("nodes") or []
            except Exception:
                await asyncio.sleep(1)

    asyncio.run(run())


threading.Thread(target=ws_thread, daemon=True).start()

pygame.init()
window = Window("Spectrum Native (spike2)", size=(600, 300))
renderer = Renderer(window, accelerated=1, vsync=1)
clock = pygame.time.Clock()
font_big = pygame.font.SysFont("monospace", 36, bold=True)

GREEN = (51, 255, 102, 255)
BG = (11, 15, 20, 255)

# Cache the CPU% text texture -- only rebuild it when the string actually
# changes, instead of re-rasterizing + re-uploading every frame.
_label_cache = {"text": None, "texture": None}


def label_texture(text):
    if _label_cache["text"] != text:
        surf = font_big.render(text, True, GREEN[:3])
        _label_cache["texture"] = Texture.from_surface(renderer, surf)
        _label_cache["text"] = text
    return _label_cache["texture"]


running = True
t = 0.0
while running:
    dt = clock.tick(60) / 1000.0
    t += dt
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            running = False

    with state_lock:
        sys_ = dict(state["system"])
        nodes = list(state["nodes"])

    renderer.draw_color = BG
    renderer.clear()
    renderer.draw_color = GREEN

    cx, cy, r = 100, 150, 60
    steps = 48
    prev = None
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        p = (cx + r * math.cos(a), cy + r * math.sin(a))
        if prev is not None:
            renderer.draw_line(prev, p)
        prev = p

    cpu_pct = sys_.get("cpu_percent", 0.0)
    tex = label_texture(f"{cpu_pct:.1f}%")
    tex.draw(dstrect=(cx - tex.width // 2, cy - tex.height // 2, tex.width, tex.height))

    ox, oy, ow, oh = 200, 20, 380, 260
    for n in nodes:
        motion = n.get("motion", 0.0)
        stale = n.get("stale", True)
        amp = (oh / 2 - 10) * (0.15 + motion) if not stale else 5
        prev = None
        for x in range(0, ow, 4):
            y = oy + oh / 2 + math.sin(t * 3 + x * 0.05) * amp
            p = (ox + x, y)
            if prev is not None:
                renderer.draw_line(prev, p)
            prev = p

    renderer.present()

pygame.quit()
