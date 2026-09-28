"""Port of web/index.html's meshWaveFrame(): a purely decorative sine
trace in the MESHTASTIC panel, not driven by real waveform data -- its
amplitude eases toward 1 when the link is up and 0 when it's down (so it
settles into a flat line on transitions instead of snapping), with a slow
~5s breath modulation on top. Driven by its own always-advancing phases,
independent of how often real Meshtastic data arrives.
"""
import math
import random

COLOR = (103, 234, 148)
STEPS = 80


class MeshWave:
    def __init__(self):
        self.online = False
        self.amp = 0.0
        self.phase = random.random() * math.pi * 2
        self.breath_phase = random.random() * math.pi * 2

    def set_online(self, online):
        self.online = online

    def draw(self, gfx, renderer, x0, y0, w, h):
        self.amp += ((1.0 if self.online else 0.0) - self.amp) * 0.02
        self.phase += 0.06
        self.breath_phase += 0.021
        breath = 0.75 + 0.25 * math.sin(self.breath_phase)
        mid_y = h / 2
        half_h = (h / 2 - 4) * self.amp * breath

        pts = []
        for i in range(STEPS + 1):
            x = (i / STEPS) * w
            y = mid_y + math.sin(self.phase + i * 0.35) * half_h
            pts.append((x0 + x, y0 + y))

        bg = (11, 15, 20)
        glow = tuple(round(b + (f - b) * 0.35) for f, b in zip(COLOR, bg)) + (255,)
        crisp = tuple(round(b + (f - b) * 0.9) for f, b in zip(COLOR, bg)) + (255,)
        gfx.draw_glow_polyline(renderer, pts, glow, crisp, offsets=gfx._GLOW_OFFSETS_THIN)
