#!/usr/bin/env python3
"""HEMS energy logger.

Samples the SolarFlow and P1 meter every 5 s and integrates power into energy
(Wh). Every 15 minutes one row with that interval's energy totals is appended
to /var/lib/hems/energy-YYYY-MM.csv (on SD, ~96 small writes per day).
Raw 5 s samples go to /run/hems/samples-YYYY-MM-DD.csv (RAM only, lost on
reboot) for debugging. A restart continues the running interval; only a power
cut loses it (up to 15 minutes).

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
import signal
import time
from datetime import datetime

import config as C
import devices

SAMPLE_S = 5
MAX_GAP_S = 30            # don't integrate across gaps longer than this
DATA_DIR = "/var/lib/hems"
RAM_DIR = "/run/hems"
RAM_STATE = f"{RAM_DIR}/logger-state.json"
DISK_STATE = f"{DATA_DIR}/logger-state.json"   # written on shutdown only
THRESH_W = 10             # below this a flow counts as zero

FIELDS = [
    "ac_in", "ac_out", "solar", "dc_in", "dc_out", "grid_import", "grid_export",
    "gc_ac_in", "gc_dc_in",            # grid_charge mode
    "sc_solar", "sc_dc_in",            # solar_charge mode
    "dc_dc_out", "dc_ac_out",          # discharge mode
    "idle_dc_out", "idle_dc_in",       # idle mode
    "idle_s", "covered_s",
    "l1", "l2", "l3",                  # net energy per phase (+ import, - export)
    "l1_max_w", "l2_max_w", "l3_max_w", # highest import sample
    "l1_over_s", "l2_over_s", "l3_over_s",  # time above the fuse rating
]


def s16(v):
    return v - 65536 if v >= 32768 else v


def sample():
    sf = devices.solarflow_report()
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
        "grid": None, "l1": None, "l2": None, "l3": None,
    }
    try:
        p1 = devices.read_p1()
        s["grid"] = p1["grid_w"]
        s["l1"], s["l2"], s["l3"] = p1["phases_w"]
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
    for n in ("l1", "l2", "l3"):
        if s.get(n) is not None:
            acc[n] += s[n] * h
            acc[n + "_max_w"] = max(acc[n + "_max_w"], s[n])
            if s[n] > C.FUSE_A * C.LINE_V:
                acc[n + "_over_s"] += dt
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


def append_csv(path, header, row):
    """Append a row; if the file has an older header, rewrite it with the new
    columns first (old rows get empty cells)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, newline="") as f:
            old = next(csv.reader(f), None)
        if old != header:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            tmp = path + ".tmp"
            with open(tmp, "w", newline="") as f:
                w = csv.DictWriter(f, header, restval="", extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(header)
        w.writerow(row)


def column(k):
    return k if k.endswith(("_s", "_w")) else k + "_wh"


def write_row(start, end, soc0, soc1, acc):
    append_csv(f"{DATA_DIR}/energy-{start:%Y-%m}.csv",
               ["start", "end", "soc_start", "soc_end"] + [column(k) for k in FIELDS],
               [start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), soc0, soc1]
               + [int(acc[k]) if k.endswith(("_s", "_w")) else round(acc[k], 2) for k in FIELDS])


def write_raw(now, s):
    path = f"{RAM_DIR}/samples-{now:%Y-%m-%d}.csv"
    if not os.path.exists(path):               # keep only today + yesterday in RAM
        for old in sorted(glob.glob(f"{RAM_DIR}/samples-*.csv"))[:-1]:
            os.remove(old)
    keys = ["ac_in", "ac_out", "solar", "dc", "grid", "soc", "rep_to_pack", "rep_from_pack", "batcur",
            "pack_soc", "ac_mode", "l1", "l2", "l3"]
    append_csv(path, ["time"] + keys, [now.isoformat(timespec="seconds")]
               + [round(s[k], 1) if k == "dc" else s[k] for k in keys])


def save_state(path, slot, soc0, soc1, acc, last_s, wall):
    """The running interval, so a restart continues it instead of losing it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"slot": slot.isoformat(), "soc0": soc0, "soc1": soc1, "acc": acc,
                   "last_s": last_s, "wall": wall}, f)
    os.replace(tmp, path)


def load_state():
    """Newest saved interval: RAM copy (crash, restart) or disk copy (reboot)."""
    best = None
    for path in (RAM_STATE, DISK_STATE):
        try:
            with open(path) as f:
                st = json.load(f)
            st["slot"] = datetime.fromisoformat(st["slot"])
            st["acc"] = {k: float(st["acc"].get(k, 0)) for k in FIELDS}
            if best is None or st["wall"] > best["wall"]:
                best = st
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return best


def slot_of(t):
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def main():
    acc = dict.fromkeys(FIELDS, 0.0)
    slot = slot_of(datetime.now().astimezone())
    soc0 = soc1 = None
    last_t, last_s = None, None

    st = load_state()
    if st and st["slot"] <= slot:
        gap = time.time() - st["wall"]
        if st["slot"] == slot:                         # continue the interval
            acc, soc0, soc1 = st["acc"], st["soc0"], st["soc1"]
            if st["last_s"] and 0 <= gap <= MAX_GAP_S:
                last_t, last_s = time.monotonic() - gap, st["last_s"]
            print(f"continuing interval {slot:%H:%M} after a {gap:.0f} s stop", flush=True)
        elif st["acc"]["covered_s"] > 0:               # it ended while we were down
            write_row(st["slot"], datetime.fromtimestamp(st["wall"]).astimezone(), st["soc0"], st["soc1"], st["acc"])
            soc1 = st["soc1"]
    for path in (RAM_STATE, DISK_STATE):
        if os.path.exists(path):
            os.remove(path)

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    wall = time.time()
    while not stopping:
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
            last_t, last_s, wall = t0, s, now.timestamp()
            soc1 = s["soc"]
            if soc0 is None:
                soc0 = soc1
            write_raw(now, s)
            save_state(RAM_STATE, slot, soc0, soc1, acc, last_s, wall)

        while not stopping and time.monotonic() - t0 < SAMPLE_S:
            time.sleep(0.2)

    save_state(DISK_STATE, slot, soc0, soc1, acc, last_s, wall)
    print("stopped, interval saved", flush=True)


if __name__ == "__main__":
    main()
