"""Tibber price fetcher with on-disk cache and a background refresh thread.

Token comes from the environment (TIBBER_TOKEN), loaded by systemd from
/etc/hems/secrets.env. Prices are 15-minute slots (total incl. tax, EUR/kWh).
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta

API = "https://api.tibber.com/v1-beta/gql"
CACHE = "/var/lib/hems/prices.json"
QUERY = """{ viewer { homes { currentSubscription { priceInfo(resolution: QUARTER_HOURLY) {
  today { total startsAt level } tomorrow { total startsAt level } } } } } }"""

_lock = threading.Lock()
_slots = []          # list of (start: datetime aware, total: float, level: str)
_fetched_at = 0.0


def _fetch():
    token = os.environ.get("TIBBER_TOKEN")
    if not token:
        raise RuntimeError("TIBBER_TOKEN not set")
    req = urllib.request.Request(
        API, data=json.dumps({"query": QUERY}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "hems-pi/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    if "errors" in d:
        raise RuntimeError(d["errors"])
    pi = d["data"]["viewer"]["homes"][0]["currentSubscription"]["priceInfo"]
    return pi["today"] + pi["tomorrow"]


def _store(raw):
    global _slots, _fetched_at
    slots = [(datetime.fromisoformat(x["startsAt"]), x["total"], x["level"]) for x in raw]
    with _lock:
        _slots, _fetched_at = slots, time.time()


def _load_cache():
    try:
        with open(CACHE) as f:
            _store(json.load(f))
    except Exception:
        pass


def _save_cache(raw):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f)
    os.replace(tmp, CACHE)


def _needs_refresh(now):
    with _lock:
        slots, age = _slots, time.time() - _fetched_at
    if not slots:
        return True
    last_day = max(s[0] for s in slots).date()
    if last_day < now.date():                       # cache is from a previous day
        return True
    if last_day == now.date() and now.hour >= 13 and age > 15 * 60:
        return True                                 # waiting for tomorrow's prices
    return age > 6 * 3600


def _worker():
    _load_cache()
    while True:
        now = datetime.now().astimezone()
        if _needs_refresh(now):
            try:
                raw = _fetch()
                _store(raw)
                _save_cache(raw)
            except Exception as e:
                print(f"tibber: fetch failed: {e}", flush=True)
                time.sleep(300)
                continue
        time.sleep(60)


def start():
    threading.Thread(target=_worker, daemon=True, name="tibber").start()


def slots():
    with _lock:
        return list(_slots)


def current(now=None):
    now = now or datetime.now().astimezone()
    for start, total, level in slots():
        if start <= now < start + timedelta(minutes=15):
            return total, level
    return None


def hourly(day):
    """Average price per hour for the given date -> list of (hour, price)."""
    acc = {}
    for start, total, _ in slots():
        if start.date() == day:
            acc.setdefault(start.hour, []).append(total)
    return [(h, sum(v) / len(v)) for h, v in sorted(acc.items())]
