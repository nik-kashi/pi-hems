#!/usr/bin/env python3
"""Read-only HEMS web page for the local network.

  GET /            the page (hems/web/index.html)
  GET /api/state   live state, plan, executor status and prices as JSON

Everything comes from files the other services already write, so the web
page never talks to the devices itself. There are no write endpoints.
"""
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
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass                                   # keep the journal quiet


if __name__ == "__main__":
    print(f"hems-web on port {PORT}", flush=True)
    Server(("::", PORT), Handler).serve_forever()
