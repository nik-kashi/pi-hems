# pi-hems

A lightweight Home Energy Management System on a Raspberry Pi 3B+.

First goal: optimise charging and discharging of a **Zendure SolarFlow 2400 Pro**
(PV + home battery) using local device data and **Tibber** dynamic prices.

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 3 Model B+ | DietPi 64-bit (Debian 13 Trixie), Ethernet only, 16 GB microSD |
| 3.5" SPI TFT (ILI9486) | 480x320, `piscreen` overlay, used in portrait as a status display (touch not used) |
| Zendure SolarFlow 2400 Pro | + 2 extra battery packs, local zenSDK HTTP API |
| Zendure Smart Meter P1 | reads the DSMR P1 port, local HTTP API, 3-phase |
| Tibber | dynamic price contract, 15-minute prices via GraphQL API |

## Architecture

```
Zendure P1 meter ──(LAN)──┐
                          ├──► Pi ──► 3.5" TFT status screen
SolarFlow 2400 Pro ─(LAN)─┘    │
                               ├──► energy log (15-min CSV)
Tibber API (prices) ──────────►┘
```

All device communication is local (`GET /properties/report`). Nothing is
written to the devices yet: the system is read-only for now.

## Components (`hems/`)

| File | Purpose |
|---|---|
| `devices.py` | Read P1 meter and SolarFlow over the local API |
| `tibber.py` | Fetch Tibber 15-min prices, cache in `/var/lib/hems/prices.json`, background refresh |
| `display.py` | Status screen: grid, solar, battery, price (+ today's hourly chart), CPU/RAM/temp; dims between sunset and sunrise |
| `logger.py` | Samples every 5 s, writes 15-min energy totals split by operating mode to `/var/lib/hems/energy-YYYY-MM.csv` |
| `report.py` | `hems-report [YYYY-MM-DD]`: measured charge/discharge efficiency, round trip, standby drain |

## Setup

1. Flash DietPi (RPi 2/3/4 ARMv8 image), headless, Ethernet, SSH key.
2. Append `boot/config.txt.snippet` to `/boot/firmware/config.txt`, reboot.
3. `apt install python3 python3-pil python3-numpy fonts-dejavu-core`
4. Enable the Zendure local API in the Zendure app (add HEMS).
5. Store the Tibber token on the Pi (root-only, never in git):
   ```bash
   ssh -t root@<pi> 'read -rsp "Tibber token: " t; echo; umask 077; mkdir -p /etc/hems; printf "TIBBER_TOKEN=%s\n" "$t" > /etc/hems/secrets.env'
   ```
6. Adjust device IPs in `hems/devices.py` and `hems/logger.py`, then `./deploy.sh root@<pi>`.

## Status

- [x] OS, display, live data, Tibber prices
- [x] Energy logger to measure real losses
- [ ] Battery wear cost model
- [ ] Control algorithm (shadow mode first, then live; replaces Zendure "Zenki" mode)
