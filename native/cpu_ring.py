"""Port of web/index.html's cpuRingFrame(): the CPU gauge isn't a plain
circle, it's a wavy ring rippling all the way around -- the loaded arc
(clockwise from 12 o'clock up to the current %) ripples at high amplitude/
frequency, the rest at a low but still-visible amplitude, plus a slow
"breath" on the amplitude and a faster pulse on the line width. Driven by
its own always-advancing phases, independent of how often real CPU data
arrives -- same reasoning as the Meshtastic wave (mesh_wave.py).
"""
import math
import random

POINTS = 160
# Web's own 40/10 cycle counts (~4 points/cycle at 160 points) look fine
# there because canvas anti-aliases the stroke -- this Renderer draws
# unaliased straight segments, so the same numbers read as a jagged star
# instead of a ripple. Toned down to stay readable as a wave at this pixel
# scale (2026-09-27); same idea, not the same numbers.
CYCLES_HIGH = 22
CYCLES_LOW = 7
HIGH_AMP = 5.5
LOW_AMP = 1.2

# 3-stop gradient (teal -> orange -> red), same stops as the web version's
# CPU_RING_STOPS / cpuLoadColor, 0-1 float RGB there, 0-255 int here.
STOPS = [
    (140, 232, 176),  # #37e8b0 @ 0%
    (255, 184, 77),   # #ffb84d @ 50%
    (255, 84, 112),   # #ff5470 @ 100%
]


def load_color(frac):
    t = max(0.0, min(1.0, frac)) * 2
    i = min(1, int(t))
    local_t = t - i
    r0, g0, b0 = STOPS[i]
    r1, g1, b1 = STOPS[i + 1]
    return (
        round(r0 + (r1 - r0) * local_t),
        round(g0 + (g1 - g0) * local_t),
        round(b0 + (b1 - b0) * local_t),
    )


class CpuRing:
    def __init__(self):
        self.frac = 0.0
        self.color = STOPS[0]
        self.phase = random.random() * math.pi * 2
        self.pulse_phase = random.random() * math.pi * 2
        self.breath_phase = random.random() * math.pi * 2

    def set_percent(self, percent):
        self.frac = max(0.0, min(1.0, percent / 100))
        self.color = load_color(self.frac)

    def draw(self, gfx, renderer, cx, cy, r_max):
        self.phase += 0.05
        self.pulse_phase += 0.062
        self.breath_phase += 0.021
        amp_breath = 1.0 + 0.5 * math.sin(self.breath_phase)
        base_r = r_max - 14

        pts = []
        for i in range(POINTS + 1):
            t = (i % POINTS) / POINTS
            angle = -math.pi / 2 + t * math.pi * 2
            loaded = t < self.frac
            amp = (HIGH_AMP if loaded else LOW_AMP) * amp_breath
            cycles = CYCLES_HIGH if loaded else CYCLES_LOW
            rr = base_r + math.sin(t * math.pi * 2 * cycles + self.phase) * amp
            pts.append((cx + math.cos(angle) * rr, cy + math.sin(angle) * rr))

        bg = (11, 15, 20)
        glow = tuple(round(b + (f - b) * 0.4) for f, b in zip(self.color, bg)) + (255,)
        crisp = tuple(round(b + (f - b) * 0.95) for f, b in zip(self.color, bg)) + (255,)
        gfx.draw_glow_polyline(renderer, pts, glow, crisp)
