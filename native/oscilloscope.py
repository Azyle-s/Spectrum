"""Port of web/index.html's oscilloscope JS (stepSignal/buildTracePath/
drawTrace) -- same constants, same per-node runtime state, same phase/
noise/spike model, so the two versions' traces behave identically, not
just "similar in spirit". See that file's own comments for the reasoning
behind each constant; this only translates the mechanism, not re-derives
it.

The three decorative traces are NOT special-cased with their own formula
(an earlier version of this file did that, and it was wrong -- the web
version's addDecorativeLine() just drops extra entries into the SAME
nodeRuntime map with stale=True permanently, so they run through the
exact same stepSignal()/drawTrace() as real nodes). Doing anything else
makes the decorative and real traces visibly move at different speeds/
intensities, since they'd be driven by two different physics models
(reported 2026-09-27). Matching web exactly here fixes that by construction.
"""
import math
import random
import time

import pygame
from pygame._sdl2.video import Texture

# Matches the web version's SAMPLE_COUNT exactly (one Path2D + one
# stroke() per trace there; here each point is a real Python-level
# draw_line() call, no batched multi-point primitive in this pygame
# version's Renderer API -- but a full app-wide CPU budget check
# (2026-09-27, post-reboot) showed ~8% total system CPU at this point
# count, with Chromium's own equivalent at ~29% on the same screen, so
# there's no real budget pressure to keep this reduced anymore -- and a
# mismatched point count was exactly why the traces scrolled at visibly
# different speeds (fewer points here = the same-width panel showing a
# shorter slice of history, i.e. faster/choppier-looking motion for the
# same real-world frequency).
SAMPLE_COUNT = 300

PALETTE = [(157, 108, 255), (55, 232, 176), (255, 184, 77), (255, 84, 112), (199, 146, 255), (244, 244, 161)]
OFFSETS_FRAC = [0, -12 / 260, 12 / 260, -24 / 260, 24 / 260, -36 / 260]
MARGIN_FRAC = 30 / 260

BASE_FREQ, FREQ_GAIN = 0.45, 3.0
BASE_AMP, AMP_GAIN = 0.14, 0.5
SPIKE_DURATION = 0.18
MIRROR_ALPHA = 0.26
STALE_ALPHA = 0.3

# id, color, offset_frac -- negative ids keep them out of range of real
# node_ids so set_nodes()'s cleanup pass never touches them, same trick
# the web version uses.
#
# Ordered darkest to brightest on purpose: draw order here is z-order
# (later = on top, same reasoning as the mirror-vs-primary fix above),
# and a dark line crossing over a bright one reads as a visible "cut" in
# the bright line, while the reverse (bright over dark) barely shows --
# pine was last (on top) and beige is by far the brightest of the three,
# so pine-over-beige was the one crossing that actually looked broken
# (reported 2026-09-27).
DECORATIVE = [
    (-3, (58, 90, 82), -36 / 260),  # dark muted pine
    (-2, (126, 200, 227), 24 / 260),  # soft sky blue
    (-1, (216, 200, 160), -24 / 260),  # beige
]


class Oscilloscope:
    def __init__(self):
        self.rt: dict[int, dict] = {}
        for i, (node_id, color, offset_frac) in enumerate(DECORATIVE):
            self.rt[node_id] = {
                "color": color,
                "offset_frac": offset_frac,
                "doubled": False,
                "samples": [0.0] * SAMPLE_COUNT,
                # Evenly spread (120 deg apart, +/- a little jitter) so
                # these reliably cross each other over the panel's ~2.3
                # visible wave cycles -- fully random phases could land
                # close enough together to look like near-parallel
                # tracks instead of distinct crossing lines, which is
                # down to luck-of-the-random-seed, not guaranteed
                # (reported 2026-09-27).
                "phase": i * (2 * math.pi / len(DECORATIVE)) + random.uniform(-0.3, 0.3),
                "smoothed_motion": 0.0,
                "target_motion": 0.0,
                "presence": False,
                "stale": True,
                "next_spike_at": 0.0,
                "spike_start": -1.0,
                "spike_amp": 0.0,
            }
        self._last_t = time.monotonic()
        self._surf = None
        self._tex = None

    def _runtime(self, node_id):
        rt = self.rt.get(node_id)
        if rt is None:
            idx = (node_id - 1 + len(PALETTE) * 100) % len(PALETTE)
            rt = {
                "color": PALETTE[idx],
                "offset_frac": OFFSETS_FRAC[idx % len(OFFSETS_FRAC)],
                "doubled": False,  # mirror trace causes the cuts, see draw() note below -- disabled until fixed
                "samples": [0.0] * SAMPLE_COUNT,
                "phase": random.random() * math.pi * 2,
                "smoothed_motion": 0.0,
                "target_motion": 0.0,
                "presence": False,
                "stale": True,
                "next_spike_at": 0.0,
                "spike_start": -1.0,
                "spike_amp": 0.0,
            }
            self.rt[node_id] = rt
        return rt

    def set_nodes(self, nodes):
        seen = set()
        for n in nodes:
            nid = n["node_id"]
            seen.add(nid)
            rt = self._runtime(nid)
            rt["target_motion"] = n.get("motion", 0.0)
            rt["presence"] = n.get("presence", False)
            rt["stale"] = n.get("stale", True)
        for nid in list(self.rt):
            if nid > 0 and nid not in seen:
                del self.rt[nid]

    def _step(self, rt, dt, now):
        rt["smoothed_motion"] += ((0.0 if rt["stale"] else rt["target_motion"]) - rt["smoothed_motion"]) * min(1, dt * 4)
        freq = BASE_FREQ + rt["smoothed_motion"] * FREQ_GAIN
        rt["phase"] += freq * dt * 2 * math.pi
        wave = math.sin(rt["phase"]) * (BASE_AMP + rt["smoothed_motion"] * AMP_GAIN)

        if rt["presence"] and not rt["stale"] and now >= rt["next_spike_at"]:
            rt["spike_start"] = now
            rt["spike_amp"] = 0.5 + rt["smoothed_motion"] * 0.5
            interval = max(0.35, 1.6 - rt["smoothed_motion"] * 1.2)
            rt["next_spike_at"] = now + interval + random.random() * 0.2
        spike = 0.0
        if rt["spike_start"] >= 0:
            frac = (now - rt["spike_start"]) / SPIKE_DURATION
            if frac >= 1:
                rt["spike_start"] = -1.0
            else:
                spike = math.sin(frac * math.pi) * rt["spike_amp"]

        value = max(-1.0, min(1.0, wave + spike))
        rt["samples"].append(value)
        rt["samples"].pop(0)

    def _points(self, samples, w, h, offset_frac, sign):
        center_y = h / 2 + offset_frac * h
        half_h = h / 2 - h * MARGIN_FRAC
        dx = w / (len(samples) - 1)
        return [(i * dx, center_y + sign * v * half_h) for i, v in enumerate(samples)]

    def draw(self, gfx, renderer, x0, y0, w, h):
        """Renders every trace onto a software Surface (pygame.draw.lines,
        the same mature CPU rasterizer TextCache already relies on for
        glyphs -- see gfx.py), then uploads that ONE Surface as a Texture
        and draws it with a single GPU-accelerated call.

        This replaces drawing every trace as ~300 individual
        renderer.draw_line()/fill_rect() calls. That approach was a
        direct port of the web version's Path2D+stroke() (cheap there,
        since canvas batches an entire path into one draw regardless of
        point count) -- but this Renderer has no equivalent batched
        multi-point primitive, so it cost one real Python/SDL call per
        segment. Several attempts at fixing visibly-gapped traces by
        changing how EACH segment gets rasterized (rounding coordinates,
        varying offset-copy counts, switching draw_line->fill_rect) had
        no effect (confirmed against the real screen, not just
        screenshots, 2026-09-27) and neither did fixing draw ORDER
        between traces -- so the actual cause was never conclusively
        pinned down. Rather than keep guessing at that renderer's
        behavior, this sidesteps it entirely with a rasterizer that
        doesn't share whatever quirk it has, using a pattern already
        proven reliable elsewhere in this codebase. Bonus: also cheaper
        (one texture upload/frame instead of ~1500+ individual draw
        calls across 6 traces).
        """
        now = time.monotonic()
        dt = min(0.05, now - self._last_t)
        self._last_t = now

        for rt in self.rt.values():
            self._step(rt, dt, now)

        iw, ih = round(w), round(h)
        if self._surf is None or self._surf.get_size() != (iw, ih):
            self._surf = pygame.Surface((iw, ih), pygame.SRCALPHA)
        surf = self._surf
        surf.fill((0, 0, 0, 0))

        # Stale/decorative traces first, live ones last -- draw order is
        # z-order (later = on top), so this keeps a dim background trace
        # from ever painting over a live one at a crossing.
        #
        # True per-pixel alpha here (surface is SRCALPHA, uploaded as a
        # blended Texture) instead of the old _blend()-against-background
        # pre-mix -- that hack existed because the Renderer's draw_color
        # alpha wasn't reliably blending; this path goes through pygame's
        # own software surface compositing instead, which does.
        def rgba(color, alpha):
            return (*color, round(max(0.0, min(1.0, alpha)) * 255))

        # Every stale/background trace (the 3 decorative ones and any real
        # node not currently reporting, e.g. node 2/3 before they're wired
        # up) shares this same lighter 3-pass stack -- thinner than the
        # original 6-pass version, uniformly, so nothing but node 1's live
        # primary trace reads as visually heavy (asked 2026-09-28).
        _THIN_OFFSETS = [(0, 0), (1, 0), (0, 1)]

        def draw_thin(color, pts):
            for ox, oy in _THIN_OFFSETS:
                pygame.draw.aalines(surf, color, False, [(x + ox, y + oy) for x, y in pts])

        ordered = sorted(self.rt.values(), key=lambda rt: not rt["stale"])
        for rt in ordered:
            alpha = STALE_ALPHA if rt["stale"] else 1.0

            # Mirror drawn BEFORE the primary, not after: pygame.draw
            # replaces pixels rather than blending onto what's there, so
            # whichever of the two is drawn last wins outright at their
            # shared zero-crossings. The mirror is always the dimmer of
            # the pair (MIRROR_ALPHA), so drawing it second was replacing
            # the bright primary with a dim pixel right at every crossing
            # -- regular, periodic notches, exactly the bug reported
            # 2026-09-27. Primary-last matches how every other trace pair
            # already behaves here (dim stale traces before the bright
            # live one), which is why only the mirror ever showed this.
            if rt["doubled"]:
                mpts = self._points(rt["samples"], w, h, rt["offset_frac"], 1)
                if rt["stale"]:
                    draw_thin(rgba(rt["color"], alpha * MIRROR_ALPHA), mpts)
                else:
                    mipts = [(round(x), round(y)) for x, y in mpts]
                    pygame.draw.lines(surf, rgba(rt["color"], alpha * MIRROR_ALPHA * 0.35), False, mipts, 6)
                    pygame.draw.lines(surf, rgba(rt["color"], alpha * MIRROR_ALPHA), False, mipts, 3)

            pts = self._points(rt["samples"], w, h, rt["offset_frac"], -1)
            if rt["stale"]:
                # aalines (via draw_thin), not lines(width=N): pygame's
                # width>1 lines have their own joint-gap quirk at sharp
                # direction changes, which started showing up as the same
                # kind of "cuts" once these traces' phases were spread
                # out enough to actually cross each other often (reported
                # 2026-09-27). Stacking a few 1px-offset aalines passes
                # instead adds weight without that failure mode.
                draw_thin(rgba(rt["color"], alpha), pts)
            else:
                # true variable line width here (unlike the Renderer's
                # draw_line), so the "glow" is a genuinely thick soft
                # pass under a crisp narrow one, not offset copies.
                ipts = [(round(x), round(y)) for x, y in pts]
                pygame.draw.lines(surf, rgba(rt["color"], alpha * 0.35), False, ipts, 5)
                pygame.draw.lines(surf, rgba(rt["color"], alpha), False, ipts, 2)

        self._tex = Texture.from_surface(renderer, surf)
        self._tex.draw(dstrect=(round(x0), round(y0), iw, ih))
