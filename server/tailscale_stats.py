"""Tailscale peer status, via the `tailscale` CLI talking to tailscaled
over its local socket (both bind-mounted read-only into the container --
see docker-compose.yml). No HTTP/API key involved: this is the same local
socket the CLI always uses, just reached from inside the container.
"""
import asyncio
import json

TAILSCALE_BIN = "/usr/bin/tailscale"


async def fetch_status() -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            TAILSCALE_BIN, "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except (OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    devices = []
    self_info = data.get("Self")
    if self_info:
        devices.append({
            "name": self_info.get("HostName"),
            "os": self_info.get("OS"),
            "online": True,  # this machine, by definition
            "self": True,
        })
    for peer in (data.get("Peer") or {}).values():
        devices.append({
            "name": peer.get("HostName"),
            "os": peer.get("OS"),
            "online": bool(peer.get("Online")),
            "self": False,
        })
    # self first, then online peers, then offline ones
    devices.sort(key=lambda d: (not d["self"], not d["online"]))

    return {
        "devices": devices,
        "online_count": sum(1 for d in devices if d["online"]),
        "total_count": len(devices),
    }
