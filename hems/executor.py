#!/usr/bin/env python3
"""HEMS executor: turns the planner's current slot into SolarFlow commands.

Modes -> device properties (always with smartMode=1, so nothing is written
to the device's flash):
  CHARGE     acMode=1 (AC input), inputLimit=planned W, outputLimit=0
  BYPASS     acMode=1, inputLimit=0, outputLimit=0   (solar still charges)
  DISCHARGE  acMode=2 (AC output), inputLimit=0, outputLimit=follows load

DISCHARGE while net metering lasts (slot "export": true): outputLimit = planned
W, whatever the house uses; the rest is exported (worth the all-in price).
No P1 data is needed for that: discharging only lowers the phase current.

DISCHARGE after that is a zero-export load follower on the P1 total:
  house  = grid_now + device_output_now      (independent of command lag)
  output = clamp(house - TARGET_GRID_W, 0, planned W)
After each change the loop waits WRITE_MIN_INTERVAL_S (device ramp + P1
delay) before correcting again; changes smaller than DEADBAND_W are skipped.

Safety:
  - plan missing/stale        -> BYPASS
  - P1 data stale             -> no zero-export discharge (output 0)
  - CHARGE is limited to the spare capacity on the battery's phase, and
    stops without P1 data (guard.py)
  - SIGTERM / exit            -> BYPASS
With EXECUTOR_DRY_RUN=True nothing is sent; commands are only logged.
"""
import json
import os
import signal
import sys
import time
from datetime import datetime

import config as C
import devices
from guard import ChargeGuard

PLAN_FILE = "/run/hems/plan.json"
STATE_FILE = "/run/hems/executor.json"


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def current_slot():
    """(mode, planned_w, reason, export) for now; BYPASS if no valid plan."""
    try:
        with open(PLAN_FILE) as f:
            plan = json.load(f)
    except Exception:
        return "BYPASS", 0, "no plan", False
    created = datetime.fromisoformat(plan["created"])
    if time.time() - created.timestamp() > C.PLAN_STALE_S:
        return "BYPASS", 0, "plan stale", False
    now = datetime.now().astimezone()
    for s in plan["slots"]:
        start = datetime.fromisoformat(s["start"])
        if start.timestamp() <= now.timestamp() < start.timestamp() + C.SLOT_MIN * 60:
            return s["mode"], s["power_w"], "plan", bool(s.get("export"))
    return "BYPASS", 0, "slot not in plan", False


class Executor:
    def __init__(self):
        self.dry = C.EXECUTOR_DRY_RUN
        self.sn = None
        self.sent = {}            # last values we commanded
        self.last_write = 0.0
        self.mode = None
        self.out_cmd = 0          # our own running output setpoint
        self.guard = ChargeGuard()

    # -- device io ----------------------------------------------------------
    def send(self, props, why):
        changed = {k: v for k, v in props.items() if self.sent.get(k) != v}
        if not changed:
            return
        tag = "DRY-RUN" if self.dry else "WRITE"
        log(f"{tag} {changed}  ({why})")
        if not self.dry:
            try:
                rsp = devices.write_solarflow(self.sn, changed)
                if isinstance(rsp, dict) and rsp.get("success") is False:
                    log(f"write rejected: {rsp}")
                    return
            except Exception as e:
                log(f"write failed: {e}")
                return
        self.sent.update(changed)
        self.last_write = time.time()

    # -- one control step ---------------------------------------------------
    def step(self):
        rep = devices.solarflow_report()
        p = rep["properties"]
        self.sn = rep["sn"]

        # device reboot restores flash values -> re-assert smartMode
        if p.get("smartMode") != 1 and not self.dry:
            self.sent.clear()      # limits were reset too, send everything again
        self.send({"smartMode": 1}, "no flash writes")

        try:
            p1 = devices.read_p1()
            p1_ok = time.time() - p1["ts"] < C.P1_STALE_S
        except Exception:
            p1, p1_ok = None, False

        self.guard.observe(time.time(), p1["phases_w"][C.BATTERY_PHASE] if p1 else None,
                           p1["msg"] if p1 else None, p["gridInputPower"], p["outputHomePower"])

        mode, planned_w, why, export = current_slot()
        if mode != self.mode:
            log(f"mode {self.mode} -> {mode} ({why}, planned {planned_w} W, SOC {p['electricLevel']}%)")
            self.mode = mode
            self.out_cmd = 0

        if mode == "CHARGE":
            limit = min(planned_w, C.CHARGE_MAX_W)
            limit, gwhy = self.guard.allow(time.time(), limit)
            why = gwhy if gwhy != "plan" else why
            self.send({"acMode": 1, "outputLimit": 0, "inputLimit": int(limit)}, why)

        elif mode == "DISCHARGE":
            cap = min(planned_w, C.DISCHARGE_MAX_W)
            if export:
                self.out_cmd = int(cap)
                self.send({"acMode": 2, "inputLimit": 0, "outputLimit": self.out_cmd},
                          f"export allowed (saldering), grid {p1['grid_w'] if p1 else '?'} W")
            elif not p1_ok:
                self.out_cmd = 0
                self.send({"acMode": 2, "inputLimit": 0, "outputLimit": 0}, "P1 stale -> no discharge")
            else:
                actual_out = p["outputHomePower"]
                house = p1["grid_w"] + actual_out          # load estimate, lag-independent
                target = int(max(0, min(cap, house - C.TARGET_GRID_W)))
                # wait until the device has ramped and the (slower) P1 has caught
                # up with the last change before correcting again; correcting
                # sooner reacts to stale data and makes the output oscillate
                settled = time.time() - self.last_write >= C.WRITE_MIN_INTERVAL_S
                if settled and abs(target - self.out_cmd) >= C.DEADBAND_W:
                    self.out_cmd = target
                self.send({"acMode": 2, "inputLimit": 0, "outputLimit": self.out_cmd},
                          f"grid {p1['grid_w']} W, out {actual_out} W")

        else:  # BYPASS
            self.send({"acMode": 1, "inputLimit": 0, "outputLimit": 0}, why)

        self.publish(mode, planned_w, p, p1, export)

    def publish(self, mode, planned_w, p, p1, export=False):
        state = {
            "ts": time.time(), "dry_run": self.dry, "mode": mode, "planned_w": planned_w,
            "export": export,
            "commanded": self.sent, "device": {"acMode": p["acMode"], "inputLimit": p["inputLimit"],
                                               "outputLimit": p["outputLimit"], "smartMode": p.get("smartMode")},
            "grid_w": p1["grid_w"] if p1 else None,
        }
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)

    def safe_stop(self, *_):
        log("stopping -> BYPASS")
        try:
            if self.sn:
                self.send({"acMode": 1, "inputLimit": 0, "outputLimit": 0}, "shutdown")
        finally:
            sys.exit(0)


def main():
    ex = Executor()
    signal.signal(signal.SIGTERM, ex.safe_stop)
    signal.signal(signal.SIGINT, ex.safe_stop)
    log(f"executor started ({'DRY-RUN' if ex.dry else 'LIVE'})")
    while True:
        t0 = time.monotonic()
        try:
            ex.step()
        except Exception as e:
            log(f"step failed: {e}")
        time.sleep(max(0, C.EXEC_INTERVAL_S - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
