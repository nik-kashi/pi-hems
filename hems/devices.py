"""Read-only access to the local Zendure devices (zenSDK HTTP API)."""
import json
import time
import urllib.request

from config import SOLARFLOW, P1_METER
TIMEOUT_S = 1.5


def _get(base):
    with urllib.request.urlopen(f"{base}/properties/report", timeout=TIMEOUT_S) as r:
        return json.load(r)


def read_p1():
    """Grid power in W (positive = import). Per-phase values in a list."""
    d = _get(P1_METER)
    return {
        "grid_w": d["total_power"],
        "phases_w": [d["a_aprt_power"], d["b_aprt_power"], d["c_aprt_power"]],
        "ts": time.time(),
    }


def read_solarflow():
    d = _get(SOLARFLOW)
    p = d["properties"]
    return {
        "soc": p["electricLevel"],                 # %
        "solar_w": p["solarInputPower"],
        "bat_charge_w": p["outputPackPower"],      # into the packs
        "bat_discharge_w": p["packInputPower"],    # out of the packs
        "grid_to_bat_w": p["gridInputPower"],      # AC charging from grid
        "to_home_w": p["outputHomePower"],         # AC output to the house
        "ac_mode": p["acMode"],                    # 1 = input (charge), 2 = output
        "min_soc": p["minSoc"] / 10,
        "max_soc": p["socSet"] / 10,
        "temp_c": (p["hyperTmp"] - 2731) / 10,
        "packs": len(d.get("packData", [])),
        "ts": time.time(),
    }


def solarflow_report():
    """Full raw report (includes the serial number needed for writes)."""
    return _get(SOLARFLOW)


def write_solarflow(sn, props):
    """POST /properties/write. Returns the parsed response (or raw text)."""
    body = json.dumps({"sn": sn, "properties": props}).encode()
    req = urllib.request.Request(f"{SOLARFLOW}/properties/write", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S * 2) as r:
        txt = r.read().decode(errors="replace")
    try:
        return json.loads(txt)
    except ValueError:
        return txt
