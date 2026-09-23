"""Pi-hole v6 stats via its REST API. Login accepts either the main admin
password or a revocable Application Password (Settings -> API -> App
Password in the Pi-hole UI) -- same /api/auth flow either way, so this can
be swapped later without a code change. Sessions expire after inactivity,
but polling counts as activity and keeps them alive; we only re-authenticate
after a restart or an actual 401.
"""
import os

import httpx

PIHOLE_HOST = os.environ.get("SPECTRUM_PIHOLE_HOST", "192.168.1.78")
PIHOLE_PORT = os.environ.get("SPECTRUM_PIHOLE_PORT", "80")
PIHOLE_PASSWORD = os.environ.get("SPECTRUM_PIHOLE_PASSWORD", "")
BASE_URL = f"http://{PIHOLE_HOST}:{PIHOLE_PORT}/api"

_sid: str | None = None


async def _login(client: httpx.AsyncClient) -> str | None:
    resp = await client.post(f"{BASE_URL}/auth", json={"password": PIHOLE_PASSWORD})
    return resp.json().get("session", {}).get("sid")


async def fetch_summary(client: httpx.AsyncClient) -> dict | None:
    global _sid
    if not PIHOLE_PASSWORD:
        return None
    if _sid is None:
        _sid = await _login(client)
        if _sid is None:
            return None

    resp = await client.get(f"{BASE_URL}/stats/summary", headers={"X-FTL-SID": _sid})
    if resp.status_code == 401:
        _sid = await _login(client)
        if _sid is None:
            return None
        resp = await client.get(f"{BASE_URL}/stats/summary", headers={"X-FTL-SID": _sid})
    if resp.status_code != 200:
        return None

    queries = resp.json().get("queries", {})
    return {
        "total": queries.get("total", 0),
        "blocked": queries.get("blocked", 0),
        "percent_blocked": queries.get("percent_blocked", 0.0),
    }
