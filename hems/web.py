#!/usr/bin/env python3
"""Read-only HEMS web page for the local network.

  GET /            the page (hems/web/index.html)
  GET /api/state   live state, plan, executor status and prices as JSON
  GET /api/history daily price/battery history for the last year as JSON

Everything comes from files the other services already write, so the web
page never talks to the devices itself. There are no write endpoints.
"""
import csv
import glob
import ipaddress
import json
import os
import socket
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HEMS_WEB_PORT", "80"))
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = {
    "state": "/run/hems/state.json",
    "plan": "/run/hems/plan.json",
    "executor": "/run/hems/executor.json",
}
PRICES = "/var/lib/hems/prices.json"
DATA_DIR = "/var/lib/hems"
HISTORY_DAYS = 366
try:
    from config import WEAR_EUR_PER_KWH
except Exception:
    WEAR_EUR_PER_KWH = 0.0


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _prices():
    raw = _load(PRICES) or []
    today = datetime.now().astimezone().date()
    keep = {today, today + timedelta(days=1)}
    return [{"start": x["startsAt"], "price": x["total"], "level": x["level"]}
            for x in raw if datetime.fromisoformat(x["startsAt"]).date() in keep]


def api_state():
    out = {k: _load(v) for k, v in FILES.items()}
    if out["plan"]:
        out["plan"]["slots"] = out["plan"]["slots"][:200]
    out["prices"] = _prices()
    out["server_time"] = time.time()
    return out


# ---- history -------------------------------------------------------------------
_file_cache = {}   # path -> (mtime, parsed)


def _cached(path, parse):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _file_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        val = parse(path)
    except (OSError, ValueError, KeyError):
        return None
    _file_cache[path] = (mtime, val)
    return val


def _parse_prices(path):
    """{slot epoch: price} plus {date: [sum, n]} for one month."""
    slots, days = {}, {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            t, p = datetime.fromisoformat(r["start"]), float(r["total"])
            slots[int(t.timestamp())] = p
            d = days.setdefault(t.date().isoformat(), [0.0, 0])
            d[0] += p
            d[1] += 1
    return slots, days


def _parse_energy(path):
    """[(slot epoch, date, grid kWh into battery, battery kWh out)] for one month."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["start"])
            chg = float(r["ac_in_wh"] or 0) / 1000
            dis = max(0.0, float(r["ac_out_wh"] or 0) - float(r["solar_wh"] or 0)) / 1000   # minus solar passing through
            slot = int(t.timestamp()) // 900 * 900
            rows.append((slot, t.date().isoformat(), chg, dis))
    return rows


def api_history():
    """One row per day: Tibber day average, grid kWh charged and battery kWh
    discharged, and the kWh-weighted price of each. Months are summed on the page."""
    today = datetime.now().astimezone().date()
    first = today - timedelta(days=HISTORY_DAYS - 1)
    months = sorted({(first + timedelta(days=i)).strftime("%Y-%m") for i in range(HISTORY_DAYS)})
    price, avg = {}, {}
    for m in months:
        got = _cached(f"{DATA_DIR}/prices-{m}.csv", _parse_prices)
        if got:
            price.update(got[0])
            avg.update(got[1])
    acc = {}
    for m in months:
        for slot, day, chg, dis in _cached(f"{DATA_DIR}/energy-{m}.csv", _parse_energy) or []:
            a = acc.setdefault(day, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])   # chg, dis, chg kWh priced, chg cost, dis kWh priced, dis value
            a[0] += chg
            a[1] += dis
            p = price.get(slot)
            if p is not None:
                a[2] += chg; a[3] += chg * p
                a[4] += dis; a[5] += dis * p
    days = []
    for i in range(HISTORY_DAYS):
        d = (first + timedelta(days=i)).isoformat()
        a, s = acc.get(d), avg.get(d)
        if not a and not s:
            continue
        row = {"date": d, "avg": round(s[0] / s[1], 4) if s else None, "live": bool(a),
               "chg": 0, "dis": 0, "chgP": None, "disP": None}
        if a:
            row.update(chg=round(a[0], 3), dis=round(a[1], 3),
                       chgP=round(a[3] / a[2], 4) if a[2] >= 0.1 else None,
                       disP=round(a[5] / a[4], 4) if a[4] >= 0.1 else None)
        days.append(row)
    return {"days": days, "wear": WEAR_EUR_PER_KWH}


def _local_client(addr):
    """Only answer the home network. IPv6 addresses are globally routable, so
    accept those only from the Pi's own /64 networks."""
    ip = ipaddress.ip_address(addr.split("%")[0])
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if ip.is_loopback or ip.is_link_local:
        return True
    if ip.version == 4:
        return ip.is_private
    try:
        with open("/proc/net/if_inet6") as f:
            return any(ip in ipaddress.ip_network((int(l.split()[0], 16) >> 64 << 64, 64)) for l in f)
    except OSError:
        return False


class Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)   # IPv4 too
        super().server_bind()

    def verify_request(self, request, client_address):
        return _local_client(client_address[0])


class Handler(BaseHTTPRequestHandler):
    server_version = "hems-web"

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(api_state()).encode(), "application/json")
        elif path == "/api/history":
            self._send(200, json.dumps(api_history()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass                                   # keep the journal quiet


if __name__ == "__main__":
    print(f"hems-web on port {PORT}", flush=True)
    Server(("::", PORT), Handler).serve_forever()
