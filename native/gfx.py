"""Reusable drawing helpers on top of pygame's SDL2 Renderer API
(pygame._sdl2.video) -- draw_line/draw_rect/fill_rect are genuinely
hardware-accelerated (confirmed ~9x cheaper than pygame.draw.* in this
project's own feasibility spike, 2026-09-26). Anything that needs
pygame.font or pygame.draw (both CPU-side software rasterization) only
ever runs once per distinct value, cached as a Texture -- never inside
the per-frame render loop.
"""
import math
import re
from collections import OrderedDict

from pygame._sdl2.video import Texture

_DSEG_RUN = re.compile(r"[\d:]+(\.\d+)?")

# Values like live clocks, per-process CPU%, or the Unbound query counter
# (monotonically increasing) change on essentially every frame -- an
# unbounded cache leaks one GPU Texture per distinct value ever seen, which
# crashed the Pi's video driver (SIGSEGV) after ~1 minute in practice
# (2026-09-26). Capped LRU keeps static labels cached while letting churny
# numeric values fall out once they're no longer the most recent value.
_MAX_ENTRIES = 256


class TextCache:
    """Renders text to a Texture once per (text, font, color), reuses it
    on every later frame that asks for the same combination, up to
    _MAX_ENTRIES distinct combinations (LRU eviction beyond that)."""

    def __init__(self, renderer):
        self.renderer = renderer
        self._cache: OrderedDict[tuple, tuple] = OrderedDict()  # (font_id, text, color) -> (Texture, rect)

    def get(self, font, text, color):
        key = (id(font), text, color)
        entry = self._cache.get(key)
        if entry is not None:
            self._cache.move_to_end(key)
            return entry
        surf = font.render(text, True, color)
        tex = Texture.from_surface(self.renderer, surf)
        entry = (tex, tex.get_rect())
        self._cache[key] = entry
        if len(self._cache) > _MAX_ENTRIES:
            self._cache.popitem(last=False)
        return entry

    def draw(self, font, text, color, x, y, anchor="topleft"):
        tex, rect = self.get(font, text, color)
        rect = rect.copy()
        setattr(rect, anchor, (round(x), round(y)))
        tex.draw(dstrect=rect)
        return rect

    def size(self, font, text):
        _, rect = self.get(font, text, (255, 255, 255))
        return rect.width, rect.height


def _digitize_segments(s):
    """Splits s into (text, is_dot) runs -- digit/colon runs (and their
    decimal point) vs everything else. Shared by measure_digitized() and
    draw_digitized() so measuring never has to actually draw (see
    measure_digitized's docstring for why that matters)."""
    segments = []  # (text, is_dot)
    pos = 0
    for m in _DSEG_RUN.finditer(s):
        if m.start() > pos:
            segments.append((s[pos:m.start()], False, False))
        for i, part in enumerate(m.group(0).split(".")):
            if i > 0:
                segments.append((".", True, True))
            if part:
                segments.append((part, True, False))
        pos = m.end()
    if pos < len(s):
        segments.append((s[pos:], False, False))
    return segments


def measure_digitized(texts, s, font_dseg, font_normal):
    """Width/height of draw_digitized(s, ...) without drawing anything --
    draw_digitized's own (text, font, color) cache key means a "throwaway"
    draw at (0, 0) just to read back its returned width is NOT actually
    throwaway, it really renders there every frame; call sites that only
    wanted the size (to right-align or center a second real draw) were
    doing that and papering the top-left corner with garbled overlapping
    text as a result (found 2026-09-27)."""
    total_w, max_h = 0, 0
    for text, is_dseg, is_dot in _digitize_segments(s):
        font = font_dseg if is_dseg else font_normal
        w, h = texts.size(font, text)
        total_w += w + (4 if is_dot else 0)
        max_h = max(max_h, h)
    return total_w, max_h


_GLOW_TEXT_OFFSETS = [(-2, 0), (2, 0), (0, -2), (0, 2)]


def draw_text_glow(texts, font, s, color, x, y, anchor="topleft", alpha=0.35):
    """Same halo technique as draw_digitized(glow=True), for a single
    plain (non-digitized) string -- the title, which isn't a mixed
    digit/unit string so doesn't go through draw_digitized at all."""
    w, h = texts.size(font, s)
    if anchor == "center":
        x, y = x - w / 2, y - h / 2
    elif anchor == "topright":
        x -= w
    for ox, oy in _GLOW_TEXT_OFFSETS:
        _draw_alpha(texts, font, s, color, x + ox, y + oy, alpha)


def draw_texture_glow(tex, dstrect, alpha=0.35):
    """Same halo technique, for an icon Texture instead of glyph text."""
    x, y, w, h = dstrect
    tex.alpha = round(alpha * 255)
    for ox, oy in _GLOW_TEXT_OFFSETS:
        tex.draw(dstrect=(round(x + ox), round(y + oy), w, h))
    tex.alpha = 255


def draw_digitized(texts, s, x, y, font_dseg, font_normal, color, anchor="topleft", glow=False):
    """Mixed-font draw mimicking the web version's digitize(): digit/colon
    runs (and their decimal point) in the DSEG7 font, everything else
    (units, words) in the normal font. DSEG7 has no glyphs outside
    digits/space/!/-/./:/deg, and its period has zero advance width (an
    LCD-overlay design, meant to sit on top of the previous digit) -- a
    plain single-font render either shows tofu for units or the digits
    visually collapse together around the invisible dot.

    glow=True adds a soft halo behind the crisp text -- 8 dim offset
    copies at partial alpha (this Renderer has no blur primitive), same
    idea as draw_glow_polyline but using real per-texture alpha, which
    (unlike this Renderer's primitive draw_color alpha) blends reliably,
    since these are cached glyph Textures. Matches the web version's
    green text-shadow glow on the same readouts (asked 2026-09-28).
    """
    segments = [(text, font_dseg if is_dseg else font_normal, is_dot)
                for text, is_dseg, is_dot in _digitize_segments(s)]

    widths, max_h = [], 0
    for text, font, is_dot in segments:
        w, h = texts.size(font, text)
        widths.append(w + (4 if is_dot else 0))
        max_h = max(max_h, h)
    total_w = sum(widths)

    if anchor == "center":
        start_x, start_y = x - total_w / 2, y - max_h / 2
    elif anchor == "topright":
        start_x, start_y = x - total_w, y
    else:
        start_x, start_y = x, y

    if glow:
        for ox, oy in _GLOW_TEXT_OFFSETS:
            cx = start_x + ox
            for (text, font, is_dot), w in zip(segments, widths):
                _draw_alpha(texts, font, text, color, cx + (2 if is_dot else 0), start_y + oy, 0.35)
                cx += w

    cx = start_x
    for (text, font, is_dot), w in zip(segments, widths):
        texts.draw(font, text, color, cx + (2 if is_dot else 0), start_y)
        cx += w
    return total_w, max_h


def draw_circle(renderer, cx, cy, r, color, segments=48, wobble=None):
    renderer.draw_color = color
    prev = None
    for i in range(segments + 1):
        a = 2 * math.pi * i / segments
        rr = r + (wobble(a) if wobble else 0)
        p = (cx + rr * math.cos(a), cy + rr * math.sin(a))
        if prev is not None:
            renderer.draw_line(prev, p)
        prev = p


def draw_polyline(renderer, points, color):
    if len(points) < 2:
        return
    renderer.draw_color = color
    for p0, p1 in zip(points, points[1:]):
        renderer.draw_line(p0, p1)


# 1px-offset copies standing in for the web version's shadowBlur bloom --
# this Renderer has no blur or variable line width primitive, so "glow" is
# faked by stacking a few dim offset copies under a crisp centered pass.
# `glow_color`/`crisp_color` must already be pre-blended solid RGB(A) (no
# real alpha compositing -- same reasoning as oscilloscope.py's _blend(),
# the pattern already proven to work on this Renderer). Used by the CPU
# ring and Meshtastic wave -- these render fine as-is, unlike the
# oscilloscope's traces (which have their own dedicated drawing path in
# oscilloscope.py, specifically so experiments there can't affect these
# two -- see that file's own notes, 2026-09-27).
# Thinned from 4 offset passes to 2 (asked 2026-09-28, same technique as
# the oscilloscope's decorative traces) -- both the CPU ring and the
# Meshtastic wave are the only callers of this function, so this thins
# both at once and cuts its per-frame draw_line count by ~40%.
_GLOW_OFFSETS = [(1, 0), (0, 1)]
# Meshtastic asked to go thinner still (2026-09-28) without also thinning
# the CPU ring -- down to no offset passes at all, just the crisp line,
# passed in by that caller only.
_GLOW_OFFSETS_THIN = []


def draw_glow_polyline(renderer, points, glow_color, crisp_color, offsets=_GLOW_OFFSETS):
    if len(points) < 2:
        return
    renderer.draw_color = glow_color
    for ox, oy in offsets:
        for p0, p1 in zip(points, points[1:]):
            renderer.draw_line((p0[0] + ox, p0[1] + oy), (p1[0] + ox, p1[1] + oy))
    renderer.draw_color = crisp_color
    for p0, p1 in zip(points, points[1:]):
        renderer.draw_line(p0, p1)


def draw_bar(renderer, x, y, w, h, pct, color, track_color, max_pct=100.0):
    """Horizontal filled bar -- MEM/DISK/PI-HOLE style gauge."""
    renderer.draw_color = track_color
    renderer.fill_rect((round(x), round(y), round(w), round(h)))
    fill_w = max(0, min(w, w * max(0.0, min(max_pct, pct)) / max_pct))
    if fill_w > 0:
        renderer.draw_color = color
        renderer.fill_rect((round(x), round(y), round(fill_w), round(h)))


def draw_vbar(renderer, x, y, w, h, pct, color, track_color, max_pct=100.0):
    """Vertical filled bar (fills from the bottom up) -- per-core CPU /
    temperature style gauge."""
    renderer.draw_color = track_color
    renderer.fill_rect((round(x), round(y), round(w), round(h)))
    fill_h = max(0, min(h, h * max(0.0, min(max_pct, pct)) / max_pct))
    if fill_h > 0:
        renderer.draw_color = color
        renderer.fill_rect((round(x), round(y + h - fill_h), round(w), round(fill_h)))


def draw_panel_border(renderer, rect, color):
    renderer.draw_color = color
    renderer.draw_rect(rect)


def draw_dot(renderer, cx, cy, r, color):
    """Small filled circle -- status dots (online/offline)."""
    renderer.draw_color = color
    for dy in range(-r, r + 1):
        dx = int((r * r - dy * dy) ** 0.5)
        renderer.draw_line((cx - dx, cy + dy), (cx + dx, cy + dy))


# --- Braille dot-matrix bars -- ports of the web version's dotBar/splitBar/
# verticalBar/verticalThermometer (same BrailleSymbols font, same bit
# patterns), so the gauges read as a dot-matrix instead of a flat filled
# rectangle, matching the web dashboard's look. ------------------------------
BRAILLE_BASE = 0x2800
_B_FULL = chr(BRAILLE_BASE | 0xFF)
_B_HALF = chr(BRAILLE_BASE | 0x47)
_B_TRACK_DOT = chr(BRAILLE_BASE | 0x40)
_B_SPLIT_CELL = chr(BRAILLE_BASE | 0x36)


# font.size()/texts.size() report the font's line-height (ascent+descent),
# which for BrailleSymbols has a lot more padding above/below the actual
# dot pattern than the dots themselves need -- stacking rows at that
# spacing (as braille_vbar/braille_vthermometer used to) left a visible
# gap between each row, reading as separate blocks instead of one bar
# (reported 2026-09-28). Packing rows by the glyph's real ink height (and
# offset) instead closes that gap. Cached per font since it never changes.
_ink_metrics_cache = {}


def _braille_ink_metrics(font):
    key = id(font)
    m = _ink_metrics_cache.get(key)
    if m is None:
        rect = font.render(_B_FULL, True, (255, 255, 255)).get_bounding_rect()
        m = (rect.top, rect.height)
        _ink_metrics_cache[key] = m
    return m


def _draw_alpha(texts, font, text, color, x, y, alpha, anchor="topleft"):
    """Like TextCache.draw, but blends the cached Texture at a given alpha
    (0-1) via the SDL Texture's own alpha channel -- set right before each
    draw call, so a shared cached texture can be reused at different
    alphas by different callers in the same frame without side effects."""
    tex, rect = texts.get(font, text, color)
    tex.alpha = round(max(0.0, min(1.0, alpha)) * 255)
    rect = rect.copy()
    setattr(rect, anchor, (round(x), round(y)))
    tex.draw(dstrect=rect)
    tex.alpha = 255
    return rect


def braille_hbar(texts, font, x, y, w, percent, color, track_color):
    """Horizontal braille dot gauge (MEM/DISK style): filled cells up to
    percent, one half-cell for the remainder fraction, dim single-dot
    track cells after that. w is the available pixel width -- the cell
    count is derived from the font's real glyph width so it always fits
    exactly, same reasoning as the web version's dotsToFit()."""
    cell_w, _ = texts.size(font, _B_FULL)
    total_dots = max(6, int(w // cell_w))
    eighths = max(0, min(total_dots * 2, round((percent / 100) * total_dots * 2)))
    full = eighths // 2
    half = eighths % 2 == 1
    cx = x
    for _ in range(full):
        texts.draw(font, _B_FULL, color, cx, y)
        cx += cell_w
    if half:
        texts.draw(font, _B_HALF, color, cx, y)
        cx += cell_w
    remaining = total_dots - full - (1 if half else 0)
    for _ in range(remaining):
        texts.draw(font, _B_TRACK_DOT, track_color, cx, y)
        cx += cell_w
    return total_dots * cell_w


def braille_split_hbar(texts, font, x, y, w, percent_red, color_red, color_blue):
    """Two-tone fully-filled bar (PI-HOLE style): blue first, red (the
    blocked share) after -- no empty track, unlike braille_hbar. Red on
    the right reads as "this much was cut off the end", which is the
    order asked for (2026-09-28); it was red-then-blue before."""
    cell_w, _ = texts.size(font, _B_SPLIT_CELL)
    total_dots = max(6, int(w // cell_w))
    red_cells = max(0, min(total_dots, round((percent_red / 100) * total_dots)))
    cx = x
    for _ in range(total_dots - red_cells):
        texts.draw(font, _B_SPLIT_CELL, color_blue, cx, y)
        cx += cell_w
    for _ in range(red_cells):
        texts.draw(font, _B_SPLIT_CELL, color_red, cx, y)
        cx += cell_w
    return total_dots * cell_w


def braille_vbar(texts, font, x, y, h, percent, color, dim_alpha=0.18):
    """Vertical stacked braille gauge (CPU core style): always the same
    full glyph, opacity ramped per row instead of swapping glyphs -- see
    the web version's verticalBar() comment for why (no half-lit glyph
    exists to represent a partial dot column at this cell size). Rows are
    packed by the glyph's ink height, not its line-height, so the bar
    reads as one continuous strip (see _braille_ink_metrics)."""
    ink_top, cell_h = _braille_ink_metrics(font)
    rows = max(4, int(h // cell_h))
    lit = max(0.0, min(1.0, percent / 100)) * rows
    for row in range(rows - 1, -1, -1):
        if row + 1 <= lit:
            alpha = 1.0
        elif row < lit:
            alpha = dim_alpha + (1 - dim_alpha) * (lit - row)
        else:
            alpha = dim_alpha
        row_y = y + (rows - 1 - row) * cell_h - ink_top
        _draw_alpha(texts, font, _B_FULL, color, x, row_y, alpha)
    return rows * cell_h


def draw_history_layer(renderer, x0, y0, w, h, history, max_value, color, fill_alpha,
                        glow_color, crisp_color, direction=1, pitch=9, dot_size=2):
    """Port of web/index.html's drawHistoryLayer(): a filled area chart
    from the midline out to each sample (braille-style dot-matrix fill,
    same look as the MEM/DISK/PI-HOLE bars, not a flat wash), plus a
    crisp glow line on top -- used for the NET graph's rx/tx layers, one
    mirrored above the midline (direction=-1) and one below (direction=1).

    Drawn as a hand-placed dot grid via the renderer, not by stacking
    Unicode Braille glyphs -- packing by ink height alone (as braille_vbar
    does) closed the gap between rows, but each sample was still its own
    glyph advancing at the sample spacing, which left the SAME kind of
    gap between adjacent samples' glyphs -- a comb of separate columns
    rather than one dot-matrix fill (reported 2026-09-28, still visible
    after that first fix). A fixed-pitch grid decoupled from the sample
    spacing sidesteps the font in both directions at once, same as
    braille_vthermometer. Samples are looked up by nearest column rather
    than interpolated -- good enough for a scrolling fill at this pitch,
    and it matches the crisp line below (also drawn from the raw
    samples, not resampled). pitch/dot_size tuned empirically for CPU
    cost (a flat per-column fill was tried and measured no cheaper at
    equal draw-call count, 2026-09-28) -- draw call COUNT is what costs
    on this Renderer, not each call's pixel area.
    """
    n = len(history)
    if n < 2:
        return
    mid_y = y0 + h / 2
    dx = w / (n - 1)
    scale = (h / 2 - 2) / max_value if max_value > 0 else 0

    cols = max(1, int(w // pitch))
    blended = tuple(round(bg + (c - bg) * fill_alpha) for c, bg in zip(color, _PANEL_BG)) + (255,)
    renderer.draw_color = blended
    for col in range(cols):
        cx = x0 + col * pitch + pitch / 2
        idx = round(col / max(1, cols - 1) * (n - 1))
        extent = direction * history[idx] * scale
        if abs(extent) < 1:
            continue
        rows = max(1, int(abs(extent) // pitch))
        for row in range(rows):
            cy = mid_y + (row * pitch if direction > 0 else -(row + 1) * pitch) + pitch / 2
            renderer.fill_rect((round(cx - dot_size / 2), round(cy - dot_size / 2), dot_size, dot_size))

    pts = [(x0 + i * dx, mid_y + direction * v * scale) for i, v in enumerate(history)]
    draw_glow_polyline(renderer, pts, glow_color, crisp_color)


_PANEL_BG = (11, 15, 20)  # shared by every procedural (non-glyph) dot fill in this file


def braille_vthermometer(renderer, x, y, h, temp_c, min_c, max_c, zone_start_c, zone_end_c,
                          cold_color, hot_color, dim_alpha=0.22, pitch=4, cols=4, dot_size=2):
    """Vertical thermometer (CPU temp style): every row is always drawn
    (dim), each row's OWN color fixed by its position on the scale (cold
    low, hot high) -- climbing temperature lights up more rows in their
    true color rather than recoloring the whole bar.

    Drawn as a hand-placed dot grid via the renderer, not by stacking
    Unicode Braille glyphs (which is how braille_vbar and every other
    gauge in this file work). That approach left both a vertical gap
    between stacked glyphs and a horizontal seam between the two
    side-by-side characters, and neither closed no matter how the
    stacking step was tuned -- both gaps come from the font's own glyph
    metrics (inter-line leading, inter-character advance), not from the
    positioning math (reported 2026-09-28, several attempts). Placing
    each dot's pixel position directly sidesteps the font entirely, so
    spacing is exactly regular by construction.

    pitch/dot_size are measured from the actual BrailleSymbols glyph
    pixels at the core gauges' font size (each dot is a crisp 2x2px
    square, ~4px center-to-center) so this reads as the same size dot,
    just 4 columns wide instead of that glyph's 2 (asked 2026-09-28).
    No real alpha blending on this Renderer for plain fills (same
    reasoning as draw_glow_polyline), so colors are pre-blended against
    the panel background instead.
    """
    rows = max(4, int(h // pitch))
    lit = max(0.0, min(1.0, (temp_c - min_c) / (max_c - min_c))) * rows
    for row in range(rows - 1, -1, -1):
        row_temp = min_c + ((row + 1) / rows) * (max_c - min_c)
        t = max(0.0, min(1.0, (row_temp - zone_start_c) / (zone_end_c - zone_start_c)))
        color = tuple(round(a + (b - a) * t) for a, b in zip(cold_color, hot_color))
        if row + 1 <= lit:
            alpha = 1.0
        elif row < lit:
            alpha = dim_alpha + (1 - dim_alpha) * (lit - row)
        else:
            alpha = dim_alpha
        blended = tuple(round(bg + (c - bg) * alpha) for c, bg in zip(color, _PANEL_BG)) + (255,)
        renderer.draw_color = blended
        cy = y + (rows - 1 - row) * pitch + pitch / 2
        for c in range(cols):
            cx = x + pitch / 2 + c * pitch
            renderer.fill_rect((round(cx - dot_size / 2), round(cy - dot_size / 2), dot_size, dot_size))
    return rows * pitch
