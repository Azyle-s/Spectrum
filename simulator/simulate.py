import argparse
import asyncio
import json
import math
import random
import socket
import time

CSI_LEN = 104  # matches the real firmware's LLTF subcarrier count


async def run_node(sock: socket.socket, dest: tuple[str, int], node_id: int, rate_hz: float, idle: bool):
    freq = random.uniform(0.05, 0.15)
    phase = random.uniform(0, math.tau)
    baseline_rssi = random.uniform(-70, -40)
    shape_phase = random.uniform(0, math.tau)
    base_shape = [round(20 * math.sin(i * 0.15 + shape_phase)) for i in range(CSI_LEN)]
    period = 1.0 / rate_hz
    t0 = time.monotonic()
    while True:
        t = time.monotonic() - t0
        if idle:
            target_motion = max(0.0, random.uniform(0.0, 0.04))
        else:
            target_motion = abs(math.sin(t * math.tau * freq + phase))
            target_motion = max(0.0, min(1.0, target_motion + random.uniform(-0.05, 0.05)))

        # fake CSI: a fixed per-node "room shape" plus jitter whose amplitude
        # scales with target_motion, so the server's diff-based motion score
        # reacts the same way it would to a real moving/still room.
        jitter_sigma = 1 + target_motion * 7
        csi = [
            max(-100, min(100, base + round(random.gauss(0, jitter_sigma))))
            for base in base_shape
        ]
        rssi = baseline_rssi + random.uniform(-3, 3)
        packet = {
            "node_id": node_id,
            "timestamp": int(t * 1000),
            "rssi": round(rssi, 1),
            "csi": csi,
        }
        sock.sendto(json.dumps(packet).encode("utf-8"), dest)
        await asyncio.sleep(period)


async def main():
    parser = argparse.ArgumentParser(description="Spectrum fake node simulator")
    parser.add_argument("--host", default="127.0.0.1", help="server host")
    parser.add_argument("--port", type=int, default=9999, help="server UDP port")
    parser.add_argument("--nodes", default="1", help="comma-separated node ids, e.g. 1,2,3")
    parser.add_argument("--rate", type=float, default=5.0, help="packets per second per node")
    parser.add_argument("--idle", action="store_true", help="force near-zero jitter (calm room) on all nodes")
    args = parser.parse_args()

    node_ids = [int(n) for n in args.nodes.split(",")]
    dest = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(f"simulating nodes {node_ids} -> {dest[0]}:{dest[1]} at {args.rate} Hz" + (" [idle]" if args.idle else ""))
    await asyncio.gather(*(run_node(sock, dest, nid, args.rate, args.idle) for nid in node_ids))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
