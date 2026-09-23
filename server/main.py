import asyncio
import json
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import docker_stats
import event_log
import meshtastic_stats
import pihole_stats
import system_stats
import tailscale_stats
import weather_stats
from state import NodeRegistry

UDP_HOST = "0.0.0.0"
UDP_PORT = 9999
BROADCAST_INTERVAL_S = 0.2
SYSTEM_STATS_INTERVAL_S = 1.0  # CPU/RAM/disk/net don't need node-rate updates
PIHOLE_STATS_INTERVAL_S = 5.0  # query counters don't move fast enough to justify 1s polling
WEATHER_STATS_INTERVAL_S = 900.0  # outdoor temp barely moves within 15 minutes
TAILSCALE_STATS_INTERVAL_S = 30.0  # peer online/offline state, not exactly fast-moving either
DOCKER_STATS_INTERVAL_S = 30.0  # container list, same cadence as tailscale peers
PROCESS_STATS_INTERVAL_S = 3.0  # scans every pid in /proc -- more often than that is wasted work on a Pi

# Nodes planned for this deployment (1 = active DevKitC-1, 2-3 reserved for
# the second DevKitC-1 / future nodes). Pre-registering them means they show
# up as "offline" in the UI instead of being invisible until wired up.
EXPECTED_NODE_IDS = [1, 2, 3]

# Only node 1's board has the LED indicator feature wired to anything
# meaningful for its install spot -- other nodes never get a command.
LED_NODE_IDS = {1}

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spectrum")

registry = NodeRegistry(expected_ids=EXPECTED_NODE_IDS)
_system_cache: dict = {}
_pihole_cache: dict = {}
_weather_cache: dict = {}
_tailscale_cache: dict = {}
_docker_cache: dict = {}
_process_cache: list = []
_http_client: httpx.AsyncClient | None = None
app = FastAPI()
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class WebSocketHub:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        message = json.dumps(payload)
        dead = []
        for ws in self._clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


hub = WebSocketHub()


class UdpIngestProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        # Nodes bind a fixed local port for their CSI socket (see firmware),
        # so the addr on each inbound packet doubles as their command
        # address -- no separate handshake needed to talk back to them.
        self._led_state: dict[int, bool] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        try:
            packet = json.loads(data.decode("utf-8"))
            node_id = int(packet["node_id"])
            csi = [int(v) for v in packet["csi"]]
            rssi = float(packet["rssi"])
            device_timestamp = int(packet["timestamp"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log.warning("dropped malformed packet from %s: %s", addr, exc)
            return
        if not csi:
            log.warning("dropped empty CSI packet from %s", addr)
            return
        registry.update_from_csi(node_id, csi, rssi, device_timestamp)

        led_active = registry.led_active(node_id)
        if node_id in LED_NODE_IDS and led_active is not None and self._led_state.get(node_id) != led_active:
            self._led_state[node_id] = led_active
            self.transport.sendto(json.dumps({"led": led_active}).encode("utf-8"), addr)


# Edge-detection state for the LOG panel -- broadcast_loop already builds a
# fresh nodes/meshtastic snapshot every tick, so comparing against the
# previous tick here is free instead of running a whole extra poll loop.
_last_node_stale: dict[int, bool] = {}
_last_mesh_online: bool | None = None


async def broadcast_loop():
    global _last_mesh_online
    while True:
        nodes = registry.snapshot()
        for n in nodes:
            nid, cur = n["node_id"], n["stale"]
            prev = _last_node_stale.get(nid)
            if prev is not None and prev != cur:
                if cur:
                    event_log.add(f"node {nid} signal perdu")
                else:
                    rssi = f" (rssi {n['rssi']:.0f}dBm)" if n["rssi"] is not None else ""
                    event_log.add(f"node {nid} connecté{rssi}")
            _last_node_stale[nid] = cur

        mesh = meshtastic_stats.snapshot()
        mesh_online = mesh.get("online")
        if _last_mesh_online is not None and mesh_online != _last_mesh_online:
            event_log.add("meshtastic " + ("reconnecté" if mesh_online else "déconnecté"))
        _last_mesh_online = mesh_online

        await hub.broadcast({
            "nodes": nodes,
            "system": _system_cache,
            "pihole": _pihole_cache,
            "meshtastic": mesh,
            "weather": _weather_cache,
            "tailscale": _tailscale_cache,
            "docker": _docker_cache,
            "processes": _process_cache,
            "log": event_log.snapshot(),
        })
        await asyncio.sleep(BROADCAST_INTERVAL_S)


async def system_stats_loop():
    while True:
        try:
            _system_cache.update(system_stats.snapshot())
        except OSError as exc:
            log.warning("system_stats read failed: %s", exc)
        await asyncio.sleep(SYSTEM_STATS_INTERVAL_S)


async def pihole_stats_loop():
    while True:
        try:
            summary = await pihole_stats.fetch_summary(_http_client)
            if summary is not None:
                _pihole_cache.update(summary)
        except httpx.HTTPError as exc:
            log.warning("pihole_stats fetch failed: %s", exc)
        await asyncio.sleep(PIHOLE_STATS_INTERVAL_S)


async def weather_stats_loop():
    while True:
        try:
            temp = await weather_stats.fetch_temperature(_http_client)
            if temp is not None:
                _weather_cache.update(temp)
        except httpx.HTTPError as exc:
            log.warning("weather_stats fetch failed: %s", exc)
        await asyncio.sleep(WEATHER_STATS_INTERVAL_S)


_last_tailscale_online: dict[str, bool] | None = None


async def tailscale_stats_loop():
    global _last_tailscale_online
    while True:
        try:
            status = await tailscale_stats.fetch_status()
            if status is not None:
                _tailscale_cache.update(status)
                online_map = {d["name"]: d["online"] for d in status["devices"] if not d.get("self")}
                if _last_tailscale_online is not None:
                    for name, online in online_map.items():
                        prev = _last_tailscale_online.get(name)
                        if prev is not None and prev != online:
                            event_log.add(f"tailscale: {name} {'connecté' if online else 'déconnecté'}")
                _last_tailscale_online = online_map
        except Exception as exc:
            log.warning("tailscale_stats fetch failed: %s", exc)
        await asyncio.sleep(TAILSCALE_STATS_INTERVAL_S)


_last_docker_names: set[str] | None = None


async def docker_stats_loop():
    global _last_docker_names
    while True:
        try:
            status = await docker_stats.fetch_containers()
            if status is not None:
                _docker_cache.update(status)
                names = {c["name"] for c in status["containers"]}
                if _last_docker_names is not None:
                    for started in sorted(names - _last_docker_names):
                        event_log.add(f"docker: {started} démarré")
                    for stopped in sorted(_last_docker_names - names):
                        event_log.add(f"docker: {stopped} arrêté")
                _last_docker_names = names
        except Exception as exc:
            log.warning("docker_stats fetch failed: %s", exc)
        await asyncio.sleep(DOCKER_STATS_INTERVAL_S)


async def process_stats_loop():
    global _process_cache
    while True:
        try:
            _process_cache = system_stats.top_processes(10)
        except OSError as exc:
            log.warning("process_stats read failed: %s", exc)
        await asyncio.sleep(PROCESS_STATS_INTERVAL_S)


@app.on_event("startup")
async def startup():
    global _http_client
    _http_client = httpx.AsyncClient(timeout=5.0)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(UdpIngestProtocol, local_addr=(UDP_HOST, UDP_PORT))
    log.info("UDP ingest listening on %s:%d", UDP_HOST, UDP_PORT)
    asyncio.create_task(broadcast_loop())
    asyncio.create_task(system_stats_loop())
    asyncio.create_task(pihole_stats_loop())
    asyncio.create_task(weather_stats_loop())
    asyncio.create_task(tailscale_stats_loop())
    asyncio.create_task(docker_stats_loop())
    asyncio.create_task(process_stats_loop())
    meshtastic_stats.start()
    event_log.add("spectrum démarré")


@app.on_event("shutdown")
async def shutdown():
    if _http_client is not None:
        await _http_client.aclose()


@app.get("/")
async def index():
    # This is meant to run as an always-on tab that's rarely, if ever,
    # reloaded -- without this, a stale cached copy can sit there showing
    # old behavior indefinitely after a deploy, with no obvious way to tell.
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/calibration/{node_id}/reset")
async def calibration_reset(node_id: int):
    registry.calib_reset(node_id)
    return {"ok": True}


@app.get("/calibration/{node_id}")
async def calibration_stats(node_id: int):
    stats = registry.calib_stats(node_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="no CSI received for this node yet")
    return stats


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.register(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(ws)
