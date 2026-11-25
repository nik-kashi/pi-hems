"""Local access to the Zendure devices (zenSDK HTTP API).

Device addresses are not trusted to stay fixed: when a device stops
answering at its known address, the local /24 network is scanned for a
device that answers like it (P1 meter: has "total_power"; SolarFlow:
"product" starts with "solarFlow"). The address found is shared with the
other HEMS services through ADDR_FILE (RAM), so only one of them needs to
scan.
"""
import ipaddress
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from config import SOLARFLOW, P1_METER

TIMEOUT_S = 1.5
ADDR_FILE = "/run/hems/addresses.json"
FAILS_BEFORE_SCAN = 3
MIN_SCAN_INTERVAL_S = 60

_lock = threading.Lock()
_addr = {"p1": P1_METER, "sf": SOLARFLOW}
_fails = {"p1": 0, "sf": 0}
_last_scan = 0.0
_addr_mtime = 0.0


def _http_get(base, timeout=TIMEOUT_S):
    with urllib.request.urlopen(f"{base}/properties/report", timeout=timeout) as r:
        return json.load(r)


def _kind_of(d):
    if isinstance(d, dict):
        if "total_power" in d:
            return "p1"
        if str(d.get("product", "")).startswith("solarFlow"):
            return "sf"
    return None


def _load_shared():
    """Pick up addresses another service found."""
    global _addr_mtime
    try:
        m = os.path.getmtime(ADDR_FILE)
        if m > _addr_mtime:
            with open(ADDR_FILE) as f:
                _addr.update(json.load(f))
            _addr_mtime = m
    except (OSError, ValueError):
        pass


def _save_shared():
    global _addr_mtime
    os.makedirs(os.path.dirname(ADDR_FILE), exist_ok=True)
    tmp = ADDR_FILE + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(_addr, f)
    os.replace(tmp, ADDR_FILE)
    _addr_mtime = os.path.getmtime(ADDR_FILE)


def scan():
    """Probe every host of the devices' /24 for a P1 meter and a SolarFlow."""
    global _last_scan
    _last_scan = time.time()
    host = urllib.parse.urlparse(_addr["sf"]).hostname or "192.168.2.1"
    net = ipaddress.ip_network(f"{host}/24", strict=False)

    def probe(ip):
        try:
            return str(ip), _kind_of(_http_get(f"http://{ip}", timeout=1.0))
        except Exception:
            return str(ip), None

    found = {}
    with ThreadPoolExecutor(max_workers=48) as pool:
        for ip, kind in pool.map(probe, net.hosts()):
            if kind and kind not in found:
                found[kind] = f"http://{ip}"
    changed = {k: v for k, v in found.items() if _addr.get(k) != v}
    if changed:
        _addr.update(changed)
        _save_shared()
        print(f"devices: new address {changed}", flush=True)
    return found


def _get(kind):
    with _lock:
        _load_shared()
        base = _addr[kind]
    try:
        d = _http_get(base)
        if _kind_of(d) != kind:
            raise RuntimeError(f"{base} is not the {kind} device")
        _fails[kind] = 0
        return d
    except Exception:
        _fails[kind] += 1
        with _lock:
            if _fails[kind] >= FAILS_BEFORE_SCAN and time.time() - _last_scan >= MIN_SCAN_INTERVAL_S:
                _load_shared()                     # maybe another service already found it
                if _addr[kind] == base:
                    scan()
        raise


def address(kind):
    return _addr[kind]


def p1_report():
    return _get("p1")


def read_p1():
    """Grid power in W (positive = import). Per-phase values in a list."""
    d = _get("p1")
    return {
        "grid_w": d["total_power"],
        "phases_w": [d["a_aprt_power"], d["b_aprt_power"], d["c_aprt_power"]],
        "msg": d.get("messageId"),                 # advances with every new reading
        "ts": time.time(),
    }


def read_solarflow():
    d = _get("sf")
    p = d["properties"]
    return {
        "soc": p["electricLevel"],                 # %
        "solar_w": p["solarInputPower"],
        "strings_w": [p[f"solarPower{i}"] for i in range(1, 7) if f"solarPower{i}" in p],
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
    return _get("sf")


def write_solarflow(sn, props):
    """POST /properties/write. Returns the parsed response (or raw text)."""
    body = json.dumps({"sn": sn, "properties": props}).encode()
    req = urllib.request.Request(f"{_addr['sf']}/properties/write", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S * 2) as r:
        txt = r.read().decode(errors="replace")
    try:
        return json.loads(txt)
    except ValueError:
        return txt
