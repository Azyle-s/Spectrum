"""Meshtastic node status via meshtastic-serial-bridge's TCP endpoint (the
same wire protocol as a directly-attached serial device, just tunneled by
socat -- see docker-compose.pi.yml). The meshtastic library is synchronous
and runs its own reader thread internally, so this module owns one
dedicated background thread and publishes into a plain dict that the
asyncio side just reads (same pattern as system_stats' shared cache: no
lock needed, dict item assignment is atomic under the GIL).
"""
import logging
import os
import threading
import time

from pubsub import pub

log = logging.getLogger("spectrum.meshtastic")

MESHTASTIC_HOST = os.environ.get("SPECTRUM_MESHTASTIC_HOST", "192.168.1.78")
MESHTASTIC_PORT = int(os.environ.get("SPECTRUM_MESHTASTIC_PORT", "4403"))
RECONNECT_DELAY_S = 5.0
POLL_INTERVAL_S = 5.0

# "signal power" for this node doesn't mean much on its own -- it's the
# local, serial-attached radio, not a remote link -- so instead we track
# the SNR of the last packet it heard from anywhere on the mesh, which is
# a real read on the current RF conditions around the gateway.
_state = {"online": False, "uptime_s": None, "snr": None}


def snapshot() -> dict:
    return dict(_state)


def _on_receive(packet=None, interface=None, **kwargs):
    snr = (packet or {}).get("rxSnr")
    if snr is not None:
        _state["snr"] = snr


def _worker():
    import meshtastic.tcp_interface

    pub.subscribe(_on_receive, "meshtastic.receive")

    while True:
        try:
            interface = meshtastic.tcp_interface.TCPInterface(
                hostname=MESHTASTIC_HOST, portNumber=MESHTASTIC_PORT
            )
            _state["online"] = True
            log.info("meshtastic connected to %s:%d", MESHTASTIC_HOST, MESHTASTIC_PORT)

            while interface.isConnected.is_set():
                node = interface.getMyNodeInfo() or {}
                uptime = node.get("deviceMetrics", {}).get("uptimeSeconds")
                if uptime is not None:
                    _state["uptime_s"] = uptime
                time.sleep(POLL_INTERVAL_S)

            interface.close()
        except Exception as exc:
            log.warning("meshtastic connection failed: %s", exc)

        _state["online"] = False
        time.sleep(RECONNECT_DELAY_S)


def start():
    threading.Thread(target=_worker, name="meshtastic-worker", daemon=True).start()
