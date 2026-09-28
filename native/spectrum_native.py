"""Spectrum, the native version -- same /ws data, same visual language
as web/index.html, rendered with pygame's accelerated SDL2 Renderer
instead of a browser engine. See README note at the bottom of this
session's work for why (Chromium ~80-90% CPU on this Pi for the same
dashboard vs. this approach's much lower cost, confirmed via a
feasibility spike before committing to this).

Built incrementally, module by module, in the same order as the web
version's #system-wrap rows.
"""
import math
import os
import sys
import time
from datetime import datetime

import pygame
from pygame._sdl2.video import Renderer, Texture, Window

sys.path.insert(0, os.path.dirname(__file__))
import gfx
import ws_client
from cpu_ring import CpuRing, load_color as cpu_load_color
from mesh_wave import MeshWave
from oscilloscope import Oscilloscope

NET_HISTORY_LEN = 40

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")

# Open-Meteo WMO weather codes -> icon name, same grouping as the web
# version's weatherIconKey() (https://open-meteo.com/en/docs).
WEATHER_ICON_KEYS = {
    0: "sun",
    1: "partly_cloudy", 2: "partly_cloudy",
    3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "drizzle", 57: "drizzle", 80: "drizzle",
    61: "rain", 63: "rain", 65: "rain", 66: "rain", 67: "rain", 81: "rain", 82: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow", 85: "snow", 86: "snow",
    95: "storm", 96: "storm", 99: "storm",
}


def weather_icon_key(code):
    return WEATHER_ICON_KEYS.get(code, "cloudy")

# Target is the kiosk screen's real portrait orientation (1024x600
# mounted vertically -> 600 wide, 1024 tall) -- same width the web
# version's responsive breakpoints were tuned against, so the 2fr/1fr
# column layout below matches what's actually deployed, not a guess.
# Shrunk by Sway's 2px floating border on each edge (4px total per axis):
# the window is deliberately NOT fullscreen/borderless (touchscreen use
# planned later, 2026-09-27) but still needs to exactly fill the 600x1024
# screen -- so the canvas itself is 4px smaller each way, and the border
# around it brings the total footprint back to exactly 600x1024.
WIDTH, HEIGHT = 596, 1020


def hx(h, a=255):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


BG = hx("0b0f14")
PANEL_BG = hx("10161d", 160)
BORDER = hx("223040")
TEXT = hx("d7e2ec")
MUTED = hx("5b7186")
GREEN = hx("33ff66")

C_TAILSCALE = hx("c792ff")
C_CPU = hx("37e8b0")
C_MEM = hx("7ec8e3")
C_DISK = hx("ffb84d")
C_UPTIME = hx("9d6cff")
C_NET = hx("7ec8e3")
C_NET_RX = hx("7ec8e3")
C_NET_TX = hx("ff9166")
C_MESH = hx("67ea94")
C_PIHOLE = hx("ff5470")
C_PROC = hx("ff6ec7")
C_LOG = hx("ffd166")
C_DOCKER = hx("2496ed")
C_UNBOUND = hx("94a3b8")
ONLINE = hx("37e8b0")

# --- layout grid: was an exact 2fr/1fr column split matching the web
# version's #system-wrap, but the right column (Tailscale/mem/disk/
# uptime/Meshtastic/Pi-hole/log/Docker) was too cramped to read on the
# real screen -- narrowed slightly in favor of the left column (asked
# 2026-09-28). No longer an exact web match by design.
PAD = 12
GAP = 12
COL2_W = round((WIDTH - 2 * PAD - GAP) * 0.70)
COL1_W = (WIDTH - 2 * PAD - GAP) - COL2_W
COL2_X = PAD
COL1_X = PAD + COL2_W + GAP


def fmt_gb(kb_or_bytes, is_kb=False):
    gb = kb_or_bytes / (1024 * 1024) if is_kb else kb_or_bytes / (1024 ** 3)
    return f"{gb:.1f} GB"


def fmt_rate(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 / 1024:.2f} MB/s"


def fmt_count(n):
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".", ",")
    return str(n)


def fmt_uptime_days(seconds):
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    if d:
        return f"{d}j {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


class Panel:
    """A bordered region with a label/value header line -- same idea as
    .sys-panel + .sys-header in the web CSS."""

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    def draw_frame(self, renderer):
        gfx.draw_panel_border(renderer, (round(self.x), round(self.y), round(self.w), round(self.h)), BORDER)

    def header(self, texts, font_label, label, label_color, font_dseg=None, value=None, value_color=GREEN):
        pad = 10
        texts.draw(font_label, label, label_color, self.x + pad, self.y + 8)
        if value is not None:
            fd = font_dseg or font_label
            w, _ = gfx.measure_digitized(texts, value, fd, font_label)
            gfx.draw_digitized(texts, value, self.x + self.w - pad - w, self.y + 8, fd, font_label, value_color)


def main():
    pygame.init()
    window = Window("Spectrum", size=(WIDTH, HEIGHT))
    renderer = Renderer(window, accelerated=0, vsync=1)
    clock = pygame.time.Clock()

    font_title = pygame.font.Font(os.path.join(FONT_DIR, "FSPxKayahD70-Regular.ttf"), 22)
    font_dseg_lg = pygame.font.Font(os.path.join(FONT_DIR, "DSEG7Classic-Bold.ttf"), 26)
    font_dseg_md = pygame.font.Font(os.path.join(FONT_DIR, "DSEG7Classic-Bold.ttf"), 17)
    font_dseg_sm = pygame.font.Font(os.path.join(FONT_DIR, "DSEG7Classic-Bold.ttf"), 12)
    font_dseg_sm2 = pygame.font.Font(os.path.join(FONT_DIR, "DSEG7Classic-Bold.ttf"), 14)
    font_dseg_xs = pygame.font.Font(os.path.join(FONT_DIR, "DSEG7Classic-Bold.ttf"), 10)
    font_label = pygame.font.SysFont("monospace", 16, bold=True)
    font_label_sm = pygame.font.SysFont("monospace", 14, bold=True)
    font_sub = pygame.font.SysFont("monospace", 15)
    font_sub_sm = pygame.font.SysFont("monospace", 14)
    font_sub_lg = pygame.font.SysFont("monospace", 18)
    font_braille = pygame.font.Font(os.path.join(FONT_DIR, "BrailleSymbols-Regular.ttf"), 14)

    # Icons rasterized once from the web version's own SVGs (same paths,
    # same colors baked in at raster time since this Renderer has no
    # reliable per-draw recolor) -- loaded once here, not per-frame.
    icons = {
        name: Texture.from_surface(renderer, pygame.image.load(os.path.join(ICON_DIR, f"{name}.png")))
        for name in (
            "sun", "partly_cloudy", "cloudy", "fog", "drizzle", "rain", "snow", "storm",
            "mesh_online", "mesh_offline",
        )
    }

    texts = gfx.TextCache(renderer)
    scope = Oscilloscope()
    cpu_ring = CpuRing()
    mesh_wave = MeshWave()
    net_rx_hist = [0.0] * NET_HISTORY_LEN
    net_tx_hist = [0.0] * NET_HISTORY_LEN
    # server broadcasts a new snapshot every BROADCAST_INTERVAL_S (0.2s,
    # server/main.py) but this render loop runs at 60fps -- pushing into
    # the history on every frame (as this used to) pushed the same value
    # ~12 times per real update, so the graph scrolled ~12x faster than
    # real time and each actual change landed as one big jump instead of
    # a smooth step (reported 2026-09-27, "le net... défile trop vite").
    # Gate pushes to the same real cadence as the data itself.
    NET_PUSH_INTERVAL_S = 0.2
    last_net_push = 0.0

    ws_client.start()

    running = True
    while running:
        clock.tick(60)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        payload = ws_client.snapshot()
        sysd = payload.get("system") or {}
        weather = payload.get("weather") or {}
        tailscale = payload.get("tailscale") or {}
        meshtastic = payload.get("meshtastic") or {}
        pihole = payload.get("pihole") or {}
        unbound = payload.get("unbound") or {}
        docker = payload.get("docker") or {}
        processes = payload.get("processes") or []
        log_entries = payload.get("log") or []
        nodes = payload.get("nodes") or []
        scope.set_nodes(nodes)

        renderer.draw_color = BG
        renderer.clear()

        # --- header ---
        dot_color = ONLINE if ws_client.is_connected() else hx("ff5470")
        gfx.draw_dot(renderer, PAD + 4, 18, 4, dot_color)
        gfx.draw_text_glow(texts, font_title, "SPECTRUM", GREEN, PAD + 14, 8)
        texts.draw(font_title, "SPECTRUM", GREEN, PAD + 14, 8)
        fresh = [n for n in nodes if not n.get("stale")]
        if fresh and fresh[0].get("rssi") is not None:
            rssi_s = f"{fresh[0]['rssi']:.0f}dBm"
            rw, _ = gfx.measure_digitized(texts, rssi_s, font_dseg_xs, font_sub_sm)
            gfx.draw_digitized(texts, rssi_s, WIDTH - PAD - rw, 12, font_dseg_xs, font_sub_sm, C_TAILSCALE)
        y = 40

        # --- oscilloscope ---
        scope_h = 150
        scope.draw(gfx, renderer, PAD, y, WIDTH - 2 * PAD, scope_h)
        y += scope_h + GAP

        # --- row: clock/weather | tailscale ---
        row_h = 90
        clock_panel = Panel(COL2_X, y, COL2_W, row_h)
        clock_panel.draw_frame(renderer)
        now = datetime.now()
        texts.draw(font_sub_lg, now.strftime("%a %d %b"), TEXT, clock_panel.x + 10, clock_panel.y + 8)
        gfx.draw_digitized(texts, now.strftime("%H:%M:%S"), clock_panel.x + 10, clock_panel.y + 32, font_dseg_lg, font_sub, GREEN)
        temp = weather.get("temp_c")
        if temp is not None:
            # Same size as the clock time (font_dseg_lg), wind/humidity
            # dropped, min/max kept below (asked 2026-09-28).
            temp_s = f"{temp:.1f}°C"
            temp_w, _ = gfx.measure_digitized(texts, temp_s, font_dseg_lg, font_sub)
            temp_x = clock_panel.x + clock_panel.w - 10 - temp_w
            # Same WMO-code icon as the web version, left of the reading,
            # scaled up to match (20 -> 30, same ratio as font_dseg_md ->
            # font_dseg_lg).
            weather_icon = icons.get(weather_icon_key(weather.get("weather_code")))
            icon_size = 30
            # Same y as the clock time, not the date (asked 2026-09-28).
            if weather_icon is not None:
                icon_rect = (round(temp_x - 8 - icon_size), clock_panel.y + 32, icon_size, icon_size)
                weather_icon.draw(dstrect=icon_rect)
            gfx.draw_digitized(texts, temp_s, temp_x, clock_panel.y + 32, font_dseg_lg, font_sub, GREEN)
            sub1 = f"{weather.get('temp_min_c', 0):.0f}°/{weather.get('temp_max_c', 0):.0f}°"
            sub1_w, _ = texts.size(font_sub_lg, sub1)
            texts.draw(font_sub_lg, sub1, TEXT, clock_panel.x + clock_panel.w - 10 - sub1_w, clock_panel.y + 64)

        ts_panel = Panel(COL1_X, y, COL1_W, row_h)
        ts_panel.draw_frame(renderer)
        ts_panel.header(texts, font_label, "TAILSCALE", C_TAILSCALE, font_dseg_sm,
                         f"{tailscale.get('online_count', 0)}/{tailscale.get('total_count', 0)}", GREEN)
        dy = 28
        # Dot vertically centered on the text's real height -- was a
        # fixed +4 offset tuned for the old, smaller font_sub_sm; grown
        # with the rest of the dashboard's text (2026-09-28) without this
        # following along, so the dot sat near the top of the now-taller
        # line instead of centered on it (reported 2026-09-28).
        _, line_h = texts.size(font_sub_sm, "Ag")
        for d in (tailscale.get("devices") or [])[:4]:
            dot_c = ONLINE if d.get("online") else MUTED
            gfx.draw_dot(renderer, ts_panel.x + 14, ts_panel.y + dy + line_h / 2, 3, dot_c)
            texts.draw(font_sub_sm, str(d.get("name", "?"))[:20], TEXT, ts_panel.x + 22, ts_panel.y + dy)
            dy += 14
        y += row_h + GAP

        # --- row: CPU | MEM/DISK/UPTIME ---
        # row_h grown to use the empty space that used to sit below the
        # last row (asked 2026-09-28) -- CPU's height and the MEM/DISK/
        # UPTIME stack's height both derive from this same row_h, so
        # they stay equal by construction, not by coincidence.
        row_h = 208
        cpu_panel = Panel(COL2_X, y, COL2_W, row_h)
        cpu_panel.draw_frame(renderer)
        cpu_panel.header(texts, font_label, "CPU", C_CPU)
        cpu_pct = sysd.get("cpu_percent", 0.0)
        cpu_ring.set_percent(cpu_pct)
        r_max = 56
        cx, cy = cpu_panel.x + cpu_panel.w // 2, cpu_panel.y + row_h // 2 + 8
        cpu_ring.draw(gfx, renderer, cx, cy, r_max)
        gfx.draw_digitized(texts, f"{cpu_pct:.1f}%", cx, cy, font_dseg_md, font_sub, GREEN, anchor="center")

        # per-core vertical braille bars, left of the ring -- anchored
        # from the header down rather than centered on the ring, so
        # growing the bar height (asked 2026-09-28, to use the row's
        # free space) doesn't creep upward into the header. The temp
        # thermometer below reuses this same core_bar_top/core_bar_h,
        # so the two stay equal length by construction.
        per_core = sysd.get("cpu_per_core") or []
        core_bar_top = cpu_panel.y + 48
        core_bar_h = 120
        core_left = cpu_panel.x + 14
        core_col_w = max(18, (cx - r_max - 18 - core_left) / max(1, len(per_core[:4])))
        for i, p in enumerate(per_core[:4]):
            core_color = cpu_load_color(max(0.0, min(1.0, p / 100)))
            ccx = core_left + i * core_col_w
            pct_s = f"{round(p)}%"
            pw, _ = texts.size(font_sub_sm, pct_s)
            texts.draw(font_sub_sm, pct_s, TEXT, ccx + (core_col_w - pw) / 2, core_bar_top - 14)
            gfx.braille_vbar(texts, font_braille, ccx + core_col_w / 2 - 4, core_bar_top, core_bar_h, p, core_color)
            label_s = f"C{i}"
            lw, _ = texts.size(font_sub_sm, label_s)
            texts.draw(font_sub_sm, label_s, TEXT, ccx + (core_col_w - lw) / 2, core_bar_top + core_bar_h + 6)

        # temp thermometer + load average, right of the ring -- mirrored
        # at the same distance from the ring's center as bar C3 is on the
        # left, so the two flanking bars read as symmetric around the
        # ring (asked 2026-09-28).
        temp_c = sysd.get("cpu_temp_c")
        c3_center_x = core_left + 3.5 * core_col_w
        # 8 = half of braille_vthermometer's own visual width at its
        # default cols=4/pitch=4 (4 * 4 / 2), to center it the same way
        # a core bar's x is centered within its column above.
        temp_x = cx + (cx - c3_center_x) - 8
        # Gap between the thermometer and the panel's right edge -- both
        # the temp readout and the load average lines below center
        # themselves in this same gap rather than left-aligning against
        # the bar (asked 2026-09-28).
        gap_left, gap_right = temp_x + 16, cpu_panel.x + cpu_panel.w - 10
        if temp_c is not None:
            gfx.braille_vthermometer(
                renderer, temp_x, core_bar_top, core_bar_h,
                temp_c, 20, 90, 65, 80, (140, 232, 176), (255, 84, 112),
            )
            # Same size as the CPU ring's own %.
            temp_s = f"{temp_c:.1f}°C"
            tw, _ = gfx.measure_digitized(texts, temp_s, font_dseg_md, font_sub)
            temp_text_x = gap_left + (gap_right - gap_left - tw) / 2
            gfx.draw_digitized(texts, temp_s, temp_text_x, core_bar_top + 4, font_dseg_md, font_sub, GREEN)
        load_avg = sysd.get("load_avg")
        if load_avg and len(load_avg) == 3:
            # Bottom of the bar, not below it -- three lines counting up
            # from a fixed margin above the bar's own bottom edge.
            base_y = core_bar_top + core_bar_h - 40
            for i, (label, val) in enumerate(zip(("1m", "5m", "15m"), load_avg)):
                s = f"{label} {val:.2f}"
                sw, _ = texts.size(font_sub_sm, s)
                sx = gap_left + (gap_right - gap_left - sw) / 2
                texts.draw(font_sub_sm, s, TEXT, sx, base_y + i * 12)

        side_w = COL1_W
        sub_h = (row_h - 2 * GAP) / 3
        mem = sysd.get("memory") or {}
        disk = sysd.get("disk") or {}
        uptime_s = sysd.get("uptime_s")

        mem_panel = Panel(COL1_X, y, side_w, sub_h)
        mem_panel.draw_frame(renderer)
        # font_dseg_xs -> font_dseg_sm for the header %, bar nudged down
        # 3px to clear the now-taller digits (asked 2026-09-28).
        mem_panel.header(texts, font_label_sm, "MEM", C_MEM, font_dseg_sm, f"{mem.get('percent', 0):.1f}%")
        gfx.braille_hbar(texts, font_braille, mem_panel.x + 9, mem_panel.y + 25, side_w - 18, mem.get("percent", 0), C_MEM, BORDER)
        texts.draw(font_sub_sm, f"{fmt_gb(mem.get('used_kb', 0), True)} / {fmt_gb(mem.get('total_kb', 0), True)}", TEXT, mem_panel.x + 9, mem_panel.y + 42)

        disk_panel = Panel(COL1_X, y + sub_h + GAP, side_w, sub_h)
        disk_panel.draw_frame(renderer)
        disk_panel.header(texts, font_label_sm, "DISK", C_DISK, font_dseg_sm, f"{disk.get('percent', 0):.1f}%")
        gfx.braille_hbar(texts, font_braille, disk_panel.x + 9, disk_panel.y + 25, side_w - 18, disk.get("percent", 0), C_DISK, BORDER)
        texts.draw(font_sub_sm, f"{fmt_gb(disk.get('used_bytes', 0))} / {fmt_gb(disk.get('total_bytes', 0))}", TEXT, disk_panel.x + 9, disk_panel.y + 42)

        up_panel = Panel(COL1_X, y + 2 * (sub_h + GAP), side_w, sub_h)
        up_panel.draw_frame(renderer)
        up_panel.header(texts, font_label_sm, "UPTIME", C_UPTIME)
        # Centered in the space below the header, bigger than the old
        # inline header value (asked 2026-09-28), rather than right-
        # aligned next to the label like MEM/DISK/PI-HOLE.
        uptime_s_str = fmt_uptime_days(uptime_s) if uptime_s is not None else "—"
        header_bottom = 26
        gfx.draw_digitized(
            texts, uptime_s_str, up_panel.x + side_w / 2, up_panel.y + header_bottom + (sub_h - header_bottom) / 2,
            font_dseg_sm2, font_sub_sm, GREEN, anchor="center",
        )
        y += row_h + GAP

        # --- row: NET+UNBOUND | MESHTASTIC+PI-HOLE ---
        net = sysd.get("network") or {}
        now_t = time.monotonic()
        if now_t - last_net_push >= NET_PUSH_INTERVAL_S:
            last_net_push = now_t
            net_rx_hist.append(net.get("rx_bps", 0.0)); net_rx_hist.pop(0)
            net_tx_hist.append(net.get("tx_bps", 0.0)); net_tx_hist.pop(0)
        # NET+UNBOUND and MESHTASTIC+PI-HOLE share one row height, split
        # differently per side but always summing back to it -- equal by
        # construction (asked 2026-09-28). UNBOUND only ever shows one
        # line, so it keeps a small fixed height and NET (the one thing
        # here that actually benefits from more room, the graph) takes
        # whatever's left.
        row_h = 168
        unbound_h = 34
        net_h = row_h - GAP - unbound_h
        mesh_h = pihole_h = (row_h - GAP) / 2
        net_panel = Panel(COL2_X, y, COL2_W, net_h)
        net_panel.draw_frame(renderer)
        net_val = f"↓{fmt_rate(net.get('rx_bps', 0.0))} ↑{fmt_rate(net.get('tx_bps', 0.0))}"
        texts.draw(font_label, "NET", C_NET, net_panel.x + 10, net_panel.y + 8)
        vw, _ = gfx.measure_digitized(texts, net_val, font_dseg_xs, font_sub_sm)
        gfx.draw_digitized(texts, net_val, net_panel.x + net_panel.w - 10 - vw, net_panel.y + 8, font_dseg_xs, font_sub_sm, GREEN)
        gx0, gy0, gw, gh = net_panel.x + 8, net_panel.y + 26, net_panel.w - 16, net_h - 34
        peak = max(max(net_rx_hist), max(net_tx_hist), 1024.0)
        bg3 = BG[:3]

        def _blend3(color, alpha):
            return tuple(round(b + (f - b) * alpha) for f, b in zip(color[:3], bg3)) + (255,)

        gfx.draw_history_layer(
            renderer, gx0, gy0, gw, gh, net_rx_hist, peak,
            C_NET_RX[:3], 0.55, _blend3(C_NET_RX, 0.35), _blend3(C_NET_RX, 0.95), direction=-1,
        )
        gfx.draw_history_layer(
            renderer, gx0, gy0, gw, gh, net_tx_hist, peak,
            C_NET_TX[:3], 0.45, _blend3(C_NET_TX, 0.35), _blend3(C_NET_TX, 0.95), direction=1,
        )

        unbound_panel = Panel(COL2_X, y + net_h + GAP, COL2_W, unbound_h)
        unbound_panel.draw_frame(renderer)
        texts.draw(font_label_sm, "UNBOUND", C_UNBOUND, unbound_panel.x + 10, unbound_panel.y + 10)
        ub_val = f"{unbound.get('cache_hit_pct', 0):.1f}% · {unbound.get('queries', 0)} req · {unbound.get('avg_recursion_ms', 0):.0f}ms"
        uw, _ = gfx.measure_digitized(texts, ub_val, font_dseg_xs, font_sub_sm)
        gfx.draw_digitized(texts, ub_val, unbound_panel.x + unbound_panel.w - 10 - uw, unbound_panel.y + 10, font_dseg_xs, font_sub_sm, GREEN)

        mesh_panel = Panel(COL1_X, y, side_w, mesh_h)
        mesh_panel.draw_frame(renderer)
        mesh_online = bool(meshtastic.get("online"))
        mesh_wave.set_online(mesh_online)
        mesh_panel.header(texts, font_label_sm, "MESHTASTIC", C_MESH)
        # Meshtastic's own router / router-with-slash connection icons,
        # in place of the plain "online"/"offline" text -- same swap as
        # the web version.
        mesh_icon = icons["mesh_online" if mesh_online else "mesh_offline"]
        mesh_icon_size = 20
        mesh_icon.draw(dstrect=(mesh_panel.x + side_w - 10 - mesh_icon_size, mesh_panel.y + 7, mesh_icon_size, mesh_icon_size))
        # Wave centered in the space between the header and the SNR line
        # (SNR pinned to the block's bottom edge, asked 2026-09-28)
        # instead of sitting right under the header.
        wave_h = 20
        header_bottom, snr_top = 26, mesh_h - 20
        wave_y = (header_bottom + snr_top) / 2 - wave_h / 2
        mesh_wave.draw(gfx, renderer, mesh_panel.x + 4, mesh_panel.y + wave_y, side_w - 8, wave_h)
        snr = meshtastic.get("snr")
        snr_s = f"{'+' if (snr or 0) > 0 else ''}{snr:.1f}dB" if snr is not None else "—"
        texts.draw(font_sub_sm, f"SNR {snr_s}", TEXT, mesh_panel.x + 9, mesh_panel.y + snr_top)

        pihole_panel = Panel(COL1_X, y + mesh_h + GAP, side_w, pihole_h)
        pihole_panel.draw_frame(renderer)
        # font_dseg_xs -> font_dseg_sm for the header %, q/blk line and bar
        # both nudged down to clear the now-taller digits (asked 2026-09-28).
        pihole_panel.header(texts, font_label_sm, "PI-HOLE", C_PIHOLE, font_dseg_sm, f"{pihole.get('percent_blocked', 0):.1f}%")
        texts.draw(font_sub_sm, f"{fmt_count(pihole.get('total', 0))} Q / {fmt_count(pihole.get('blocked', 0))} BLK", TEXT, pihole_panel.x + 9, pihole_panel.y + 30)
        gfx.braille_split_hbar(texts, font_braille, pihole_panel.x + 9, pihole_panel.y + 46, side_w - 18,
                                pihole.get("percent_blocked", 0), C_PIHOLE, C_NET_RX)

        y += row_h + GAP

        # --- row: PROCESSUS | LOG+DOCKER ---
        # Same shared-height trick as the row above: PROCESSUS and the
        # LOG+DOCKER stack both sum to row_h exactly (asked 2026-09-28).
        # The extra room also goes into wider per-line spacing in all
        # three lists below (was the most cramped part of the dashboard).
        row_h = 304
        proc_h = row_h
        log_h, docker_h = 156, 136
        proc_panel = Panel(COL2_X, y, COL2_W, proc_h)
        proc_panel.draw_frame(renderer)
        proc_panel.header(texts, font_label, "PROCESSUS", C_PROC)
        py = proc_panel.y + 26
        for p in processes[:10]:
            texts.draw(font_sub_sm, str(p["name"])[:18], TEXT, proc_panel.x + 10, py)
            mem_s = f"{p['mem_kb'] / 1024:.0f}MB"
            pct_s = f"{p['cpu_percent']:.1f}%"
            pw, _ = gfx.measure_digitized(texts, pct_s, font_dseg_xs, font_sub_sm)
            gfx.draw_digitized(texts, pct_s, proc_panel.x + proc_panel.w - 10 - pw, py, font_dseg_xs, font_sub_sm, TEXT)
            mw, _ = texts.size(font_sub_sm, mem_s)
            texts.draw(font_sub_sm, mem_s, TEXT, proc_panel.x + proc_panel.w - 60 - mw, py)
            py += 27

        log_panel = Panel(COL1_X, y, side_w, log_h)
        log_panel.draw_frame(renderer)
        log_panel.header(texts, font_label_sm, "LOG", C_LOG)
        ly = log_panel.y + 24
        # Max chars sized to what's actually left after the timestamp,
        # not a fixed slice -- the fixed [:16] was tuned for the old,
        # smaller font_sub_sm and started running text past the panel's
        # right edge once that font grew (reported 2026-09-28).
        char_w, _ = texts.size(font_sub_sm, "M")
        for e in log_entries[:6]:
            t = time.strftime("%H:%M", time.localtime(e["ts"]))
            texts.draw(font_sub_sm, t, C_LOG, log_panel.x + 9, ly)
            tw, _ = texts.size(font_sub_sm, t)
            text_x = log_panel.x + 12 + tw
            max_chars = max(0, int((log_panel.x + side_w - 9 - text_x) // char_w))
            texts.draw(font_sub_sm, str(e["text"])[:max_chars], TEXT, text_x, ly)
            ly += 21

        docker_panel = Panel(COL1_X, y + log_h + GAP, side_w, docker_h)
        docker_panel.draw_frame(renderer)
        docker_panel.header(texts, font_label_sm, "DOCKER", C_DOCKER, font_dseg_xs, str(docker.get("count", "—")))
        dky = docker_panel.y + 24
        for c in (docker.get("containers") or [])[:6]:
            gfx.draw_dot(renderer, docker_panel.x + 14, dky + line_h / 2, 3, ONLINE)
            texts.draw(font_sub_sm, str(c["name"])[:18], TEXT, docker_panel.x + 22, dky)
            dky += 17

        renderer.present()

    pygame.quit()


if __name__ == "__main__":
    main()
