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

All device communication is local (`GET /properties/report`,
`POST /properties/write`). Writes always set `smartMode=1` so the device
does not write them to flash. `EXECUTOR_DRY_RUN` in `config.py` decides
whether commands are actually sent.

## Planner

Each 15-minute slot gets one mode:

| Mode | Battery |
|---|---|
| CHARGE | charge from the grid |
| BYPASS | hold; house runs on grid (solar may still charge) |
| DISCHARGE | self-use, cover house load, never export |

A charge/discharge pair is only worth it when

```
discharge price > charge price / (eta_charge * eta_discharge) + wear
```

The planner solves this over the whole known price horizon (up to ~44 h after
13:00) as a linear program and re-plans every slot with the measured SOC.

## Components (`hems/`)

| File | Purpose |
|---|---|
| `devices.py` | Read P1 meter and SolarFlow over the local API |
| `tibber.py` | Fetch Tibber 15-min prices, cache in `/var/lib/hems/prices.json`, background refresh |
| `display.py` | Status screen: grid, solar, battery, price (+ today's hourly chart), CPU/RAM/temp; dims between sunset and sunrise |
| `logger.py` | Samples every 5 s, writes 15-min energy totals split by operating mode to `/var/lib/hems/energy-YYYY-MM.csv` |
| `config.py` | All tunables: device IPs, battery capacity/limits, efficiencies, wear cost, shadow mode |
| `planner.py` | Linear-program battery plan over all known 15-min prices: CHARGE / BYPASS / DISCHARGE per slot; `hems-plan` prints it |
| `executor.py` | Turns the current plan slot into SolarFlow commands (`acMode`, `inputLimit`, `outputLimit`, always `smartMode=1`); zero-export load follower while discharging; dry-run by default |
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
- [x] Planner v1 (LP, shadow mode): plan + decision log, shown on the TFT
- [ ] Measured efficiencies and confirmed battery capacity / wear cost
- [x] Executor (dry-run): commands logged, not sent
- [x] P1 export sign verified (export = negative `total_power`)
- [ ] Go live: app mode without control (not Zenki / self-consumption), `EXECUTOR_DRY_RUN = False`
- [ ] Load forecast instead of fixed house load

## Findings

- **Zendure app modes overwrite local commands.** In the app's self-consumption
  mode the device's own loop writes `acMode`/`outputLimit` (with `smartMode=1`)
  and overrides local BYPASS within ~12 s and CHARGE within ~22 s. A hybrid
  (app self-consumption + local charge/hold) is not possible; the app must be
  in a mode that does not control the battery. (Test 2026-11-24, `tools/zcheck.py`.)
- Native self-consumption reacts to load steps in 10-20 s and keeps the grid
  within about +-15 W in steady state.
