"""Feasibility spike, not the real thing yet: does a native Pygame client
actually cost less CPU than Chromium for Spectrum's heaviest bit (a
continuously-animated 60fps canvas)? Connects to the real /ws endpoint,
renders a CPU ring + a node-driven wave, nothing else -- just enough to
get an honest CPU measurement before committing to a full rebuild.
"""
import asyncio
import json
import math
import os
import threading

import pygame
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
screen = pygame.display.set_mode((600, 300))
pygame.display.set_caption("Spectrum Native (spike)")
clock = pygame.time.Clock()
font_big = pygame.font.SysFont("monospace", 36, bold=True)

GREEN = (51, 255, 102)
BG = (11, 15, 20)

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

    screen.fill(BG)

    with state_lock:
        sys_ = dict(state["system"])
        nodes = list(state["nodes"])

    cx, cy, r = 100, 150, 60
    cpu_pct = sys_.get("cpu_percent", 0.0)
    pygame.draw.circle(screen, GREEN, (cx, cy), r, width=2)
    label = font_big.render(f"{cpu_pct:.1f}%", True, GREEN)
    screen.blit(label, label.get_rect(center=(cx, cy)))

    ox, oy, ow, oh = 200, 20, 380, 260
    pygame.draw.rect(screen, (30, 40, 50), (ox, oy, ow, oh), width=1)
    for n in nodes:
        motion = n.get("motion", 0.0)
        stale = n.get("stale", True)
        amp = (oh / 2 - 10) * (0.15 + motion) if not stale else 5
        points = []
        for x in range(0, ow, 4):
            y = oy + oh / 2 + math.sin(t * 3 + x * 0.05) * amp
            points.append((ox + x, y))
        if len(points) > 1:
            pygame.draw.lines(screen, GREEN, False, points, 2)

    pygame.display.flip()

pygame.quit()
