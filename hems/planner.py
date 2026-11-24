#!/usr/bin/env python3
"""HEMS battery planner (v1).

Every slot (15 min) the battery is in one of three modes:
  CHARGE     charge from the grid
  BYPASS     hold; house runs on grid (solar may still charge)
  DISCHARGE  self-use: cover house load, never export

The plan is a linear program over all slots with known Tibber prices:
  minimise   sum p_t*c_t  -  sum (p_t - wear)*d_t  -  V_end * (SOC_end - SOC_now)
  subject to SOC_t = SOC_now + sum_{j<=t} (eta_c*c_j - d_j/eta_d)
             MIN_SOC <= SOC_t <= MAX_SOC
             0 <= c_t <= CHARGE_MAX,  0 <= d_t <= min(DISCHARGE_MAX, house load)
(c, d = AC energy per slot in kWh, SOC in kWh at the cells.)

A charge/discharge pair is only chosen when
  discharge price > charge price / (eta_c * eta_d) + wear
which is exactly the break-even rule, applied across the whole horizon.

Re-planned every slot with the measured SOC, so forecast errors self-correct.

Usage:
  planner.py            run as service (shadow mode: plan + log, no device writes)
  planner.py --print    compute one plan now and print it
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
from scipy.optimize import linprog

import config as C
import devices
import tibber

PLAN_FILE = "/run/hems/plan.json"
DECISION_DIR = "/var/lib/hems"
H = C.SLOT_MIN / 60  # hours per slot


def slot_start(t):
    return t.replace(minute=t.minute - t.minute % C.SLOT_MIN, second=0, microsecond=0)


def make_plan(now, soc_pct, slots):
    """Return plan dict for the slots from the current one to the last known price."""
    cur = slot_start(now)
    slots = [s for s in slots if s[0] >= cur]
    n = len(slots)
    if n == 0:
        return None
    prices = np.array([s[1] for s in slots])

    cap = C.CAPACITY_KWH
    e_now = soc_pct / 100 * cap
    e_min = min(C.MIN_SOC / 100 * cap, e_now)   # never infeasible if already below
    e_max = max(C.MAX_SOC / 100 * cap, e_now)
    eta_c, eta_d, wear = C.ETA_CHARGE, C.ETA_DISCHARGE, C.WEAR_EUR_PER_KWH

    # remaining fraction of the current slot
    frac = np.ones(n)
    frac[0] = max(0.05, 1 - (now - cur).total_seconds() / (C.SLOT_MIN * 60))

    c_max = C.CHARGE_MAX_W / 1000 * H * frac
    d_max = min(C.DISCHARGE_MAX_W, C.HOUSE_LOAD_W) / 1000 * H * frac

    v_end = max(0.0, float(np.quantile(prices, C.END_VALUE_QUANTILE)) * eta_d - wear)

    # x = [c_0..c_{n-1}, d_0..d_{n-1}]
    cost = np.concatenate([prices - v_end * eta_c, -(prices - wear) + v_end / eta_d])

    L = np.tril(np.ones((n, n)))                 # cumulative sum
    A_soc = np.hstack([eta_c * L, -L / eta_d])   # SOC_t - e_now
    A_ub = np.vstack([A_soc, -A_soc])
    b_ub = np.concatenate([np.full(n, e_max - e_now), np.full(n, e_now - e_min)])
    bounds = [(0, c) for c in c_max] + [(0, d) for d in np.broadcast_to(d_max, (n,))]

    res = linprog(cost, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError(f"LP failed: {res.message}")

    c, d = res.x[:n], res.x[n:]
    soc_kwh = e_now + A_soc @ res.x
    out = []
    for i, (start, price, level) in enumerate(slots):
        dur_h = H * frac[i]
        cw, dw = c[i] / dur_h * 1000, d[i] / dur_h * 1000
        if cw >= C.MIN_ACTION_W:
            mode, power = "CHARGE", round(cw)
        elif dw >= C.MIN_ACTION_W:
            mode, power = "DISCHARGE", round(dw)
        else:
            mode, power = "BYPASS", 0
        out.append({"start": start.isoformat(), "price": price, "level": level,
                    "mode": mode, "power_w": power,
                    "soc_end": round(100 * soc_kwh[i] / cap, 1)})

    grid_saving = float(prices @ d - prices @ c)
    return {
        "created": now.isoformat(timespec="seconds"),
        "soc_now": soc_pct,
        "shadow": C.SHADOW_MODE,
        "v_end": round(v_end, 4),
        "charged_kwh": round(float(c.sum()), 2),
        "discharged_kwh": round(float(d.sum()), 2),
        "grid_saving_eur": round(grid_saving, 2),
        "wear_eur": round(float(d.sum()) * wear, 2),
        "slots": out,
    }


def next_change(plan):
    """(mode, start) of the first slot whose mode differs from the current one."""
    s = plan["slots"]
    for x in s[1:]:
        if x["mode"] != s[0]["mode"]:
            return x["mode"], x["start"]
    return None


def write_plan(plan):
    os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
    tmp = PLAN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(plan, f)
    os.replace(tmp, PLAN_FILE)


def log_decision(plan, sf):
    """One row per slot: what we planned vs. what the device actually did."""
    s0 = plan["slots"][0]
    start = datetime.fromisoformat(s0["start"])
    path = f"{DECISION_DIR}/decisions-{start:%Y-%m}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["slot_start", "price", "plan_mode", "plan_power_w", "soc",
                        "dev_ac_mode", "dev_ac_in_w", "dev_ac_out_w", "dev_solar_w", "shadow"])
        w.writerow([s0["start"], s0["price"], s0["mode"], s0["power_w"], plan["soc_now"],
                    sf["ac_mode"], sf["grid_to_bat_w"], sf["to_home_w"], sf["solar_w"],
                    int(plan["shadow"])])


def print_plan(plan):
    print(f"plan @ {plan['created']}  SOC {plan['soc_now']}%  "
          f"charge {plan['charged_kwh']} kWh, discharge {plan['discharged_kwh']} kWh, "
          f"grid saving €{plan['grid_saving_eur']} - wear €{plan['wear_eur']}  "
          f"(end value €{plan['v_end']}/kWh)")
    prev = None
    for s in plan["slots"]:
        if s["mode"] != prev or s["mode"] != "BYPASS":
            t = datetime.fromisoformat(s["start"])
            print(f"  {t:%a %H:%M}  {s['price']:.3f}  {s['mode']:<9} {s['power_w']:>5} W  -> SOC {s['soc_end']:5.1f}%")
        prev = s["mode"]


def run_once():
    tibber._load_cache()
    if not tibber.slots():
        tibber._store(tibber._fetch())
    sf = devices.read_solarflow()
    return make_plan(datetime.now().astimezone(), sf["soc"], tibber.slots())


def main():
    tibber.start()
    time.sleep(5)                     # let the price cache load
    last_slot, last_nslots = None, 0
    while True:
        now = datetime.now().astimezone()
        cur = slot_start(now)
        nslots = len(tibber.slots())
        if cur != last_slot or nslots != last_nslots:
            try:
                sf = devices.read_solarflow()
                plan = make_plan(now, sf["soc"], tibber.slots())
                if plan:
                    write_plan(plan)
                    if cur != last_slot:
                        log_decision(plan, sf)
                    s0 = plan["slots"][0]
                    print(f"{now:%H:%M} SOC {sf['soc']}% price {s0['price']:.3f} -> {s0['mode']} {s0['power_w']} W"
                          f" | horizon {len(plan['slots'])} slots, saving €{plan['grid_saving_eur']}", flush=True)
                    last_slot, last_nslots = cur, nslots
            except Exception as e:
                print(f"plan failed: {e}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    if "--print" in sys.argv:
        print_plan(run_once())
    else:
        main()
