"""Unbound resolver stats, via its own remote-control protocol (the
`unbound-control` CLI's wire format) talking directly to the unbound
container's control port on the pihole stack's bridge network -- no CLI
binary needed in this container, it's a trivial plaintext line protocol
once TLS-certificate auth is off (see docker/pihole/unbound/unbound.conf
on the Pi for that tradeoff).
"""
import asyncio
import os

UNBOUND_HOST = os.environ.get("SPECTRUM_UNBOUND_HOST", "172.30.0.2")
UNBOUND_PORT = int(os.environ.get("SPECTRUM_UNBOUND_PORT", "8953"))


async def fetch_stats() -> dict | None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(UNBOUND_HOST, UNBOUND_PORT), timeout=3.0
        )
    except (OSError, asyncio.TimeoutError):
        return None

    try:
        writer.write(b"UBCT1 stats_noreset\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=3.0)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()

    values: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        key, _, val = line.partition("=")
        if key.startswith("total."):
            values[key] = val

    try:
        queries = int(values.get("total.num.queries", 0))
        hits = int(values.get("total.num.cachehits", 0))
        misses = int(values.get("total.num.cachemiss", 0))
        recursive = int(values.get("total.num.recursivereplies", 0))
        avg_recursion_ms = float(values.get("total.recursion.time.avg", 0.0)) * 1000
    except ValueError:
        return None

    return {
        "queries": queries,
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_pct": round(100.0 * hits / queries, 1) if queries else 0.0,
        "recursive_replies": recursive,
        "avg_recursion_ms": round(avg_recursion_ms, 1),
    }
