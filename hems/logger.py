#!/usr/bin/env python3
"""HEMS energy logger.

Samples the SolarFlow and P1 meter every 5 s and integrates power into energy
(Wh). Every 15 minutes one row with that interval's energy totals is appended
to /var/lib/hems/energy-YYYY-MM.csv (on SD, ~96 small writes per day).
Raw 5 s samples go to /run/hems/samples-YYYY-MM-DD.csv (RAM only, lost on
reboot) for debugging.

Energy is split by operating mode, so per-path efficiencies can be computed:
  grid_charge  : AC from grid -> cells       (no solar)
  solar_charge : solar DC     -> cells       (no AC flow)
  discharge    : cells        -> AC to house (no solar)
  idle         : nothing flowing -> cell drain = standby use
  mixed        : anything else (counted in totals only)
"""
import csv
import glob
import json
import os
import time
import urllib.request
from datetime import datetime

from config import SOLARFLOW, P1_METER
SAMPLE_S = 5
MAX_GAP_S = 30            # don't integrate across gaps longer than this
DATA_DIR = "/var/lib/hems"
RAM_DIR = "/run/hems"
THRESH_W = 10             # below this a flow counts as zero

FIELDS = [
    "ac_in", "ac_out", "solar", "dc_in", "dc_out", "grid_import", "grid_export",
    "gc_ac_in", "gc_dc_in",            # grid_charge mode
    "sc_solar", "sc_dc_in",            # solar_charge mode
    "dc_dc_out", "dc_ac_out",          # discharge mode
    "idle_dc_out", "idle_dc_in",       # idle mode
    "idle_s", "covered_s",
]


def get(base):
    with urllib.request.urlopen(f"{base}/properties/report", timeout=2) as r:
        return json.load(r)


def s16(v):
    return v - 65536 if v >= 32768 else v


def sample():
    sf = get(SOLARFLOW)
    p = sf["properties"]
    # measured DC power at the cells, + = charging, - = discharging
    dc = sum(x["totalVol"] / 100 * s16(x["batcur"]) / 10 for x in sf["packData"])
    s = {
        "ac_in": p["gridInputPower"],
        "ac_out": p["outputHomePower"],
        "solar": p["solarInputPower"],
        "dc": dc,
        "soc": p["electricLevel"],
        "rep_to_pack": p["outputPackPower"],
        "rep_from_pack": p["packInputPower"],
        "batcur": "/".join(str(x["batcur"]) for x in sf["packData"]),
        "pack_soc": "/".join(str(x["socLevel"]) for x in sf["packData"]),
        "ac_mode": p["acMode"],
        "grid": None,
    }
    try:
        s["grid"] = get(P1_METER)["total_power"]
    except Exception:
        pass
    return s


def accumulate(acc, s, dt):
    h = dt / 3600
    ac_in, ac_out, solar = s["ac_in"], s["ac_out"], s["solar"]
    dc_in, dc_out = max(s["dc"], 0), max(-s["dc"], 0)
    acc["ac_in"] += ac_in * h
    acc["ac_out"] += ac_out * h
    acc["solar"] += solar * h
    acc["dc_in"] += dc_in * h
    acc["dc_out"] += dc_out * h
    if s["grid"] is not None:
        acc["grid_import"] += max(s["grid"], 0) * h
        acc["grid_export"] += max(-s["grid"], 0) * h
    acc["covered_s"] += dt

    on = lambda w: w > THRESH_W
    if on(ac_in) and not on(solar) and not on(ac_out) and on(dc_in):
        acc["gc_ac_in"] += ac_in * h
        acc["gc_dc_in"] += dc_in * h
    elif on(solar) and not on(ac_in) and not on(ac_out) and on(dc_in):
        acc["sc_solar"] += solar * h
        acc["sc_dc_in"] += dc_in * h
    elif on(dc_out) and on(ac_out) and not on(solar) and not on(ac_in):
        acc["dc_dc_out"] += dc_out * h
        acc["dc_ac_out"] += ac_out * h
    elif not on(ac_in) and not on(ac_out) and not on(solar):
        acc["idle_dc_out"] += dc_out * h
        acc["idle_dc_in"] += dc_in * h
        acc["idle_s"] += dt


def write_row(start, end, soc0, soc1, acc):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = f"{DATA_DIR}/energy-{start:%Y-%m}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["start", "end", "soc_start", "soc_end"] + [k + ("" if k.endswith("_s") else "_wh") for k in FIELDS])
        w.writerow([start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), soc0, soc1]
                   + [round(acc[k], 2) if not k.endswith("_s") else int(acc[k]) for k in FIELDS])


def write_raw(now, s):
    os.makedirs(RAM_DIR, exist_ok=True)
    path = f"{RAM_DIR}/samples-{now:%Y-%m-%d}.csv"
    new = not os.path.exists(path)
    if new:                                    # keep only today + yesterday in RAM
        for old in sorted(glob.glob(f"{RAM_DIR}/samples-*.csv"))[:-1]:
            os.remove(old)
    keys = ["ac_in", "ac_out", "solar", "dc", "grid", "soc", "rep_to_pack", "rep_from_pack", "batcur", "pack_soc", "ac_mode"]
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time"] + keys)
        w.writerow([now.isoformat(timespec="seconds")] + [round(s[k], 1) if k == "dc" else s[k] for k in keys])


def slot_of(t):
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def main():
    acc = dict.fromkeys(FIELDS, 0.0)
    slot = slot_of(datetime.now().astimezone())
    soc0 = soc1 = None
    last_t, last_s = None, None
    while True:
        t0 = time.monotonic()
        now = datetime.now().astimezone()

        if slot_of(now) != slot:                       # close the 15-min interval
            if acc["covered_s"] > 0:
                write_row(slot, now, soc0, soc1, acc)
            acc = dict.fromkeys(FIELDS, 0.0)
            slot, soc0 = slot_of(now), soc1

        try:
            s = sample()
        except Exception as e:
            print(f"sample failed: {e}", flush=True)
            last_t = None
        else:
            if last_t is not None and last_s is not None:
                dt = t0 - last_t
                if dt <= MAX_GAP_S:
                    accumulate(acc, last_s, dt)        # previous sample held over dt
            last_t, last_s = t0, s
            soc1 = s["soc"]
            if soc0 is None:
                soc0 = soc1
            write_raw(now, s)

        time.sleep(max(0, SAMPLE_S - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
