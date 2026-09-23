import json
import os
import time
from collections import deque
from pathlib import Path

STALE_AFTER_S = 5.0

# --- Motion/presence classifier -------------------------------------------
# Calibrated 2026-09-22 on node 1, single session/position -- recalibrate
# once the second DevKitC-1 is in place, and whenever the node is remounted
# somewhere new (see server/calibration_v2.json for the raw session data).
#
# The signal: `window_std` is the per-subcarrier amplitude std across a
# short (~0.5s) rolling window of raw CSI frames -- how noisy the channel
# looks *right now*. Its *own* variability over a longer (~1s) window turned
# out to separate the three real-world states far better than its level did
# (level is dominated by node/body position, not by movement):
#   empty room            -> std(window_std) ~0.13
#   present, active-on-spot -> std(window_std) ~0.55
#   present, walking/relocating -> std(window_std) ~1.62
# A frame-to-frame diff and a short/long-term drift metric were also tried
# and dropped: diff is noise-dominated (amplifies 100Hz receiver jitter,
# suppresses slow real motion), and drift didn't separate walking from
# baseline in testing (its short vs long EMA gap stayed flat either way).
#
# `activity` below is that std(window_std) value. `presence` is a direct
# threshold on activity (movement/displacement detected, not just
# occupancy) -- MOVING_MIN alone, untouched by anything below.
#
# `motion` is the *displayed* amplitude, kept as a separate scale from the
# presence threshold on purpose: motion_scale is set above moving_min so
# that just-crossed-the-threshold movement and a strong, fast displacement
# aren't both a flat 1.0 -- there's headroom for real intensity to show.
# motion_gamma>1 then squashes the low end harder than the high end, so
# light activity (arm/head) reads as a small ripple rather than looking
# nearly as "loud" as real movement. Both are cosmetic only, applied after
# the presence check -- they change how it looks, not what gets detected.
#
# These signal-processing window sizes stay global (not per-node): they're
# about matching a human gait cycle (~1s), not about any one node's RF
# environment. Everything that *is* node-specific -- empty_max, moving_min,
# motion_scale, motion_gamma, led_min -- lives in node_thresholds.json
# instead, because calibrating a node's thresholds is pointless if every
# node is then forced to share one global value anyway (RSSI/position vary
# node to node, and -- as discovered the hard way -- so does the ambient
# noise floor by time of day; see calibration_v2.json).
CSI_WINDOW_SIZE = int(os.environ.get("SPECTRUM_CSI_WINDOW_SIZE", 50))  # ~0.5s at ~100Hz
STD_WINDOW_SIZE = int(os.environ.get("SPECTRUM_STD_WINDOW_SIZE", 50))  # ~0.5s at ~100Hz

_THRESHOLDS_PATH = Path(__file__).resolve().parent / "node_thresholds.json"
_DEFAULT_THRESHOLDS = {
    "empty_max": 0.3,
    "moving_min": 0.5,
    "motion_scale": 1.0,
    "motion_gamma": 2.5,
    "led_min": None,
}


def _load_thresholds() -> dict[str, dict]:
    try:
        with open(_THRESHOLDS_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"default": dict(_DEFAULT_THRESHOLDS)}
    raw.pop("_comment", None)
    default = {**_DEFAULT_THRESHOLDS, **raw.get("default", {})}
    return {"default": default, **{k: v for k, v in raw.items() if k != "default"}}


_THRESHOLDS = _load_thresholds()


def thresholds_for(node_id: int) -> dict:
    """Per-node thresholds, falling back to 'default' field by field so a
    node's override only needs to list what it's actually changing."""
    node_key = str(node_id)
    overrides = _THRESHOLDS.get(node_key, {})
    return {**_THRESHOLDS["default"], **overrides}


def _window_std(window: "deque[list[int]]") -> float:
    """Per-subcarrier std across the frames currently in the window,
    averaged across subcarriers."""
    n = len(window)
    if n < 2:
        return 0.0
    csi_len = len(window[0])
    total = 0.0
    for i in range(csi_len):
        values = [frame[i] for frame in window]
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        total += variance ** 0.5
    return total / csi_len


def _std(values: "deque[float]") -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return variance ** 0.5


class RunningStats:
    """Online mean/variance (Welford's algorithm) over a metric's samples,
    accumulated since the last reset -- used to capture a calibration pass
    (empty room / still person / moving person) without storing every sample."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.count = 0
        self.mean = 0.0
        self._m2 = 0.0
        self.min: float | None = None
        self.max: float | None = None
        self.started_at = time.monotonic()

    def add(self, x: float) -> None:
        self.count += 1
        delta = x - self.mean
        self.mean += delta / self.count
        self._m2 += delta * (x - self.mean)
        self.min = x if self.min is None else min(self.min, x)
        self.max = x if self.max is None else max(self.max, x)

    @property
    def std(self) -> float:
        return (self._m2 / self.count) ** 0.5 if self.count > 1 else 0.0

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "duration_s": round(time.monotonic() - self.started_at, 1),
            "mean": round(self.mean, 5),
            "std": round(self.std, 5),
            "min": round(self.min, 5) if self.min is not None else None,
            "max": round(self.max, 5) if self.max is not None else None,
        }


class NodeRegistry:
    def __init__(self, expected_ids: list[int] | None = None):
        self._nodes: dict[int, dict] = {}
        self._csi_windows: dict[int, "deque[list[int]]"] = {}
        self._std_history: dict[int, "deque[float]"] = {}
        self._calib_stats: dict[int, dict[str, RunningStats]] = {}
        for node_id in expected_ids or []:
            self._nodes[node_id] = self._placeholder(node_id)

    @staticmethod
    def _placeholder(node_id: int) -> dict:
        # Pre-registered but never received a packet yet: shows up as an
        # offline node in the UI instead of being invisible until wired up.
        return {
            "node_id": node_id,
            "csi": None,
            "motion": 0.0,
            "presence": False,
            "led_active": False,
            "rssi": None,
            "device_timestamp": None,
            "last_seen": None,
        }

    def _calib_for(self, node_id: int) -> dict[str, RunningStats]:
        return self._calib_stats.setdefault(
            node_id, {"window_std": RunningStats(), "activity": RunningStats()}
        )

    def update_from_csi(self, node_id: int, csi: list[int], rssi: float, device_timestamp: int) -> None:
        window = self._csi_windows.setdefault(node_id, deque(maxlen=CSI_WINDOW_SIZE))
        window.append(csi)
        window_std = _window_std(window)

        std_history = self._std_history.setdefault(node_id, deque(maxlen=STD_WINDOW_SIZE))
        std_history.append(window_std)
        activity = _std(std_history)

        calib = self._calib_for(node_id)
        calib["window_std"].add(window_std)
        calib["activity"].add(activity)

        t = thresholds_for(node_id)
        motion = min(1.0, activity / t["motion_scale"]) ** t["motion_gamma"]
        led_active = t["led_min"] is not None and activity >= t["led_min"]

        self._nodes[node_id] = {
            "node_id": node_id,
            "csi": csi,
            "motion": motion,
            "presence": activity >= t["moving_min"],
            "led_active": led_active,
            "rssi": rssi,
            "device_timestamp": device_timestamp,
            "last_seen": time.monotonic(),
        }

    def presence(self, node_id: int) -> bool | None:
        node = self._nodes.get(node_id)
        return node["presence"] if node else None

    def led_active(self, node_id: int) -> bool | None:
        node = self._nodes.get(node_id)
        return node["led_active"] if node else None

    def calib_reset(self, node_id: int) -> None:
        self._calib_stats[node_id] = {"window_std": RunningStats(), "activity": RunningStats()}

    def calib_stats(self, node_id: int) -> dict | None:
        calib = self._calib_stats.get(node_id)
        if not calib:
            return None
        out = {name: stats.to_dict() for name, stats in calib.items()}
        out["thresholds"] = thresholds_for(node_id)
        return out

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for node in self._nodes.values():
            never_seen = node["last_seen"] is None
            age = float("inf") if never_seen else now - node["last_seen"]
            out.append(
                {
                    "node_id": node["node_id"],
                    "motion": round(node["motion"], 4),
                    "presence": node["presence"],
                    "rssi": node["rssi"],
                    "stale": never_seen or age > STALE_AFTER_S,
                    "age_s": None if never_seen else round(age, 1),
                }
            )
        out.sort(key=lambda n: n["node_id"])
        return out
