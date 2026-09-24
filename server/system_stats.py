"""Host system stats (CPU/RAM/disk/network), read directly from the host's
/proc and root filesystem -- both bind-mounted read-only into the container
(see docker-compose.yml). No psutil: psutil hardcodes /proc on Linux and
can't be pointed at a relocated mount, and parsing these few files by hand
is a handful of lines, so a dependency isn't worth it.
"""
import os
import time

HOST_PROC = "/host/proc"
HOST_ROOT = "/hostfs"
NET_IFACE = "eth0"
# Hardware sensors under /sys reflect real host state even in an
# unprivileged container (unlike /proc, this one isn't namespaced away),
# so this needs no bind mount / HOST_ prefix -- verified directly against
# the container.
THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"

_prev_cpu_jiffies: tuple[int, int] | None = None  # (idle, total)
_prev_core_jiffies: dict[str, tuple[int, int]] = {}  # core label -> (idle, total)
_prev_net: tuple[float, int, int] | None = None  # (timestamp, rx_bytes, tx_bytes)


def _read_cpu_jiffies() -> tuple[int, int]:
    with open(f"{HOST_PROC}/stat") as f:
        first_line = f.readline()
    # "cpu  user nice system idle iowait irq softirq steal guest guest_nice"
    fields = [int(x) for x in first_line.split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    total = sum(fields)
    return idle, total


def cpu_percent() -> float:
    global _prev_cpu_jiffies
    idle, total = _read_cpu_jiffies()
    if _prev_cpu_jiffies is None:
        _prev_cpu_jiffies = (idle, total)
        return 0.0
    prev_idle, prev_total = _prev_cpu_jiffies
    _prev_cpu_jiffies = (idle, total)
    delta_total = total - prev_total
    delta_idle = idle - prev_idle
    if delta_total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total))


def per_core_percent() -> list[float]:
    global _prev_core_jiffies
    cores: list[tuple[str, int, int]] = []
    with open(f"{HOST_PROC}/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            label = line.split()[0]
            if label == "cpu":
                continue  # aggregate line, handled by cpu_percent()
            fields = [int(x) for x in line.split()[1:]]
            idle = fields[3] + fields[4]
            total = sum(fields)
            cores.append((label, idle, total))

    new_prev: dict[str, tuple[int, int]] = {}
    percents = []
    for label, idle, total in cores:
        prev = _prev_core_jiffies.get(label)
        new_prev[label] = (idle, total)
        if prev is None:
            percents.append(0.0)
            continue
        prev_idle, prev_total = prev
        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        percents.append(
            max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total)) if delta_total > 0 else 0.0
        )
    _prev_core_jiffies = new_prev
    return percents


_CLK_TCK = os.sysconf("SC_CLK_TCK")  # jiffies/second -- same unit /proc/[pid]/stat's utime+stime use
_CPU_COUNT = os.cpu_count() or 1
_prev_proc_jiffies: dict[int, tuple[float, int]] = {}  # pid -> (wall_ts, utime+stime)


def _read_process(pid: str) -> dict | None:
    try:
        with open(f"{HOST_PROC}/{pid}/stat") as f:
            stat = f.read()
        # comm (field 2) is parenthesized and can itself contain spaces or
        # parens, so split on the *last* ')' rather than whitespace -- the
        # remaining numeric fields are unambiguous from there.
        fields = stat[stat.rindex(")") + 2:].split()
        utime, stime = int(fields[11]), int(fields[12])  # state is fields[0]
        rss_kb = 0
        with open(f"{HOST_PROC}/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
        with open(f"{HOST_PROC}/{pid}/comm") as f:
            name = f.read().strip()
    except (OSError, ValueError, IndexError):
        return None
    return {"name": name, "jiffies": utime + stime, "rss_kb": rss_kb}


def top_processes(n: int = 5) -> list[dict]:
    global _prev_proc_jiffies
    now = time.monotonic()
    new_prev: dict[int, tuple[float, int]] = {}
    results = []
    for entry in os.listdir(HOST_PROC):
        if not entry.isdigit():
            continue
        pid = int(entry)
        info = _read_process(entry)
        if info is None:
            continue  # process exited between listdir() and the reads above

        new_prev[pid] = (now, info["jiffies"])
        cpu_pct = 0.0
        prev = _prev_proc_jiffies.get(pid)
        if prev is not None:
            prev_ts, prev_jiffies = prev
            dt = now - prev_ts
            if dt > 0:
                # Divided by core count so this sits on the same 0-100% scale
                # as cpu_percent() above (the CPU ring) -- without it, a
                # process pinned to one core reads relative to *that* core
                # alone (matches `top`'s default, but confusing next to a
                # ring that's already an all-core average).
                cpu_pct = max(0.0, 100.0 * (info["jiffies"] - prev_jiffies) / _CLK_TCK / dt / _CPU_COUNT)
        results.append({
            "name": info["name"],
            "pid": pid,
            "cpu_percent": round(cpu_pct, 1),
            "mem_kb": info["rss_kb"],
        })

    _prev_proc_jiffies = new_prev
    results.sort(key=lambda p: p["cpu_percent"], reverse=True)
    return results[:n]


def cpu_temp_c() -> float | None:
    try:
        with open(THERMAL_ZONE) as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return None


def memory() -> dict:
    values = {}
    with open(f"{HOST_PROC}/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            values[key] = int(rest.strip().split()[0])  # kB
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = total - available
    return {
        "total_kb": total,
        "used_kb": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def disk() -> dict:
    st = os.statvfs(HOST_ROOT)
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    used = total - free
    return {
        "total_bytes": total,
        "used_bytes": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def network() -> dict:
    global _prev_net
    rx_bytes = tx_bytes = 0
    with open(f"{HOST_PROC}/net/dev") as f:
        for line in f:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            if iface.strip() != NET_IFACE:
                continue
            fields = rest.split()
            rx_bytes = int(fields[0])
            tx_bytes = int(fields[8])

    now = time.monotonic()
    if _prev_net is None:
        _prev_net = (now, rx_bytes, tx_bytes)
        return {"rx_bps": 0.0, "tx_bps": 0.0}
    prev_ts, prev_rx, prev_tx = _prev_net
    dt = now - prev_ts
    _prev_net = (now, rx_bytes, tx_bytes)
    if dt <= 0:
        return {"rx_bps": 0.0, "tx_bps": 0.0}
    return {
        "rx_bps": max(0.0, (rx_bytes - prev_rx) / dt),
        "tx_bps": max(0.0, (tx_bytes - prev_tx) / dt),
    }


def load_average() -> list[float]:
    with open(f"{HOST_PROC}/loadavg") as f:
        parts = f.read().split()
    return [float(parts[0]), float(parts[1]), float(parts[2])]


def uptime_seconds() -> float:
    with open(f"{HOST_PROC}/uptime") as f:
        return float(f.read().split()[0])


def snapshot() -> dict:
    temp = cpu_temp_c()
    return {
        "cpu_percent": round(cpu_percent(), 1),
        "cpu_per_core": [round(p, 1) for p in per_core_percent()],
        "cpu_temp_c": round(temp, 1) if temp is not None else None,
        "load_avg": load_average(),
        "uptime_s": uptime_seconds(),
        "memory": memory(),
        "disk": disk(),
        "network": network(),
    }
