# Spectrum

A little always-on dashboard for my homelab. Runs on a Raspberry Pi, shows up as one
browser tab (or a dedicated screen), and mixes actually-useful stuff (CPU, RAM, disk,
network, running processes, Docker containers) with a few modules I added because they
were fun to build: a clock/weather widget, Tailscale/Pi-hole/Meshtastic status, a
scrolling event log, and the main gimmick — a green oscilloscope-style display fed by
ESP32 boards doing WiFi motion sensing (CSI). No cameras, no PIR sensors, just wifi
signal noise turned into "someone's in the room."

Nothing here is trying to be a serious product. It's a personal project that grew one
"wouldn't it be cool if" at a time.

## What's actually in it

- **CSI oscilloscope** — the reason this exists. ESP32-S3 boards report raw WiFi
  channel state info over UDP; the server turns that into a motion/presence score per
  node, the browser draws it as a scrolling scope trace.
- **System monitor** — CPU (per-core + temp), RAM, disk, network throughput, uptime,
  top processes. Read straight from `/proc`, no `psutil`.
- **Docker** — whatever containers are running on the host, via the Docker socket.
- **Tailscale** — your tailnet's peers, online/offline, via the local `tailscaled`
  socket.
- **Pi-hole** — query/block stats, if you point it at a Pi-hole instance.
- **Meshtastic** — connection status + SNR, if you've got a node bridged in.
- **Clock + weather** — local time and outdoor temperature (Open-Meteo, no API key).
- **Log** — a running feed of "node connected", "container started", that kind of
  thing, mostly there for the retro-terminal feel.

Every integration is optional and just shows `—` if it's not configured — Pi-hole,
Tailscale, Meshtastic and the CSI nodes all degrade gracefully if you don't have that
particular piece of hardware/software sitting around.

## How it's put together

- `server/` — a small FastAPI app. One WebSocket (`/ws`) broadcasts a JSON snapshot of
  everything ~5x/second; a UDP socket on `:9999` ingests CSI packets from the nodes.
- `web/` — single-file HTML/CSS/JS frontend, Canvas + a couple of self-hosted fonts,
  no build step, no framework.
- `firmware/console_test/` — the ESP32-S3 firmware (based on Espressif's esp-csi
  example) that turns a dev board into a sensing node.
- `simulator/` — fakes UDP CSI packets so you can see the oscilloscope move without
  owning the hardware.

## Running it

You need Docker on whatever box you're running this on (a Raspberry Pi is what it's
built for, but nothing here is Pi-specific).

```bash
git clone git@github.com:Azyle-s/Spectrum.git
cd Spectrum
cp docker-compose.pi.yml docker-compose.yml
```

Open `docker-compose.yml` and adjust it for your setup:

- `SPECTRUM_PIHOLE_HOST` / `SPECTRUM_MESHTASTIC_HOST` — point these at wherever those
  live on your network, or just delete the panels' code if you don't have them.
- `SPECTRUM_WEATHER_LAT` / `SPECTRUM_WEATHER_LON` — defaults to somewhere in northern
  France; override these (env vars, not in the compose file's checked-in defaults) for
  your own location.
- The bind mounts for `/proc`, `/`, the Tailscale socket and the Docker socket are
  what let the container see the *host's* stats instead of its own — drop whichever
  ones you don't need along with the matching panel.

If you want Pi-hole stats, create a `.env` file next to `docker-compose.yml`:

```
SPECTRUM_PIHOLE_PASSWORD=your-pihole-app-password
```

Then:

```bash
docker compose up -d --build
```

and open `http://<host>:8000`.

### Trying it without any ESP32 hardware

The oscilloscope stays flat without CSI packets coming in. Fake some — no extra
dependencies, it's stdlib only:

```bash
python simulator/simulate.py --host <server-ip> --nodes 1,2 --rate 5
```

Add `--idle` for a calm/empty-room signal instead of constant fake movement.

### Flashing an actual sensing node

`firmware/console_test/` is an ESP-IDF project (targets ESP32-S3). Set your WiFi and
server details via `idf.py menuconfig` under "Spectrum node config" (SSID, password,
server host/port, node ID) — `sdkconfig.defaults` ships with those blanked out on
purpose, don't commit your real ones back in. Then the usual:

```bash
idf.py set-target esp32s3
idf.py build flash monitor
```

Per-node motion/presence thresholds live in `server/node_thresholds.json` — it's meant
to be hand-edited and doesn't need a rebuild of the firmware, just a
`docker compose up -d --build` of the server to pick it up.

## Should you use this?

Probably not as-is — it's wired to my specific network, my specific Pi-hole, my
specific two ESP32 boards duct-taped to a wall. But if you've got similar junk lying
around and want to steal ideas or code, go for it.
