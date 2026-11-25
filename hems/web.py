#!/usr/bin/env python3
"""Read-only HEMS web page for the local network.

  GET /            the page (hems/web/index.html)
  GET /api/state   live state, plan, executor status and prices as JSON

Everything comes from files the other services already write, so the web
page never talks to the devices itself. There are no write endpoints.
"""
import json
import os
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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
