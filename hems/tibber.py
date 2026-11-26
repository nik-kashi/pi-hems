"""Tibber price fetcher with on-disk cache and a background refresh thread.

Token comes from the environment (TIBBER_TOKEN), loaded by systemd from
/etc/hems/secrets.env. Prices are 15-minute slots (total incl. tax, EUR/kWh).

With start(archive=True) (the planner only, so one process writes) every price
is also kept in /var/lib/hems/prices-YYYY-MM.csv for the web page's history.
Missing history (first run, or after the Pi was off) is fetched from Tibber's
priceInfoRange, up to ARCHIVE_DAYS back.
"""
import base64
import csv
import glob
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta

API = "https://api.tibber.com/v1-beta/gql"
CACHE = "/var/lib/hems/prices.json"
ARCHIVE_DIR = "/var/lib/hems"
ARCHIVE_DAYS = 366
RANGE_QUERY = """{ viewer { homes { currentSubscription {
  priceInfoRange(resolution: QUARTER_HOURLY, first: %d, after: "%s") { nodes { startsAt total energy } } } } } }"""
QUERY = """{ viewer { homes { currentSubscription { priceInfo(resolution: QUARTER_HOURLY) {
  today { total energy startsAt level } tomorrow { total energy startsAt level } } } } } }"""

_lock = threading.Lock()
_slots = []          # list of (start: datetime aware, total: float, level: str)
_fetched_at = 0.0


def _query(query):
    token = os.environ.get("TIBBER_TOKEN")
    if not token:
        raise RuntimeError("TIBBER_TOKEN not set")
    req = urllib.request.Request(
        API, data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "hems-pi/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if "errors" in d:
        raise RuntimeError(d["errors"])
    return d["data"]["viewer"]["homes"][0]["currentSubscription"]


def _fetch():
    pi = _query(QUERY)["priceInfo"]
    return pi["today"] + pi["tomorrow"]


def _store(raw, fetched_at=None):
    global _slots, _fetched_at
    slots = [(datetime.fromisoformat(x["startsAt"]), x["total"], x["level"]) for x in raw]
    with _lock:
        _slots, _fetched_at = slots, fetched_at or time.time()


def _load_cache():
    try:
        with open(CACHE) as f:
            _store(json.load(f), fetched_at=os.path.getmtime(CACHE))
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


# ---- price archive ------------------------------------------------------------
def _archive_path(month):
    return f"{ARCHIVE_DIR}/prices-{month}.csv"


def read_archive(month):
    """{startsAt ISO: (total, energy or None)} for one 'YYYY-MM' month file.
    total = all-in price, energy = its market part without energy tax/VAT."""
    try:
        with open(_archive_path(month), newline="") as f:
            return {r["start"]: (float(r["total"]), float(r["energy"]) if r.get("energy") else None)
                    for r in csv.DictReader(f)}
    except (OSError, KeyError, ValueError):
        return {}


def archive(raw):
    """Merge Tibber price nodes ({startsAt, total, energy}) into the monthly files."""
    by_month = {}
    for x in raw:
        t = datetime.fromisoformat(x["startsAt"]).isoformat()     # one spelling per slot
        by_month.setdefault(t[:7], {})[t] = (x["total"], x.get("energy"))
    for month, new in by_month.items():
        cur = read_archive(month)
        for k, (total, energy) in new.items():
            if energy is None and k in cur:        # never lose a known energy part
                energy = cur[k][1]
            new[k] = (total, energy)
        if all(cur.get(k) == v for k, v in new.items()):
            continue
        cur.update(new)
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        tmp = _archive_path(month) + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["start", "total", "energy"])
            for k in sorted(cur):
                w.writerow([k, cur[k][0], "" if cur[k][1] is None else cur[k][1]])
        os.replace(tmp, _archive_path(month))


def _archive_upgrade():
    """Archives written before the energy column existed are fetched again."""
    old = []
    for path in glob.glob(f"{ARCHIVE_DIR}/prices-????-??.csv"):
        with open(path) as f:
            if "energy" not in f.readline():
                old.append(path)
    for path in old:
        os.remove(path)
    if old:
        print(f"tibber: refetching {len(old)} archive months (adding the energy price)", flush=True)


def _archive_latest():
    files = sorted(glob.glob(f"{ARCHIVE_DIR}/prices-????-??.csv"))
    for path in reversed(files):
        days = read_archive(os.path.basename(path)[7:14])
        if days:
            return datetime.fromisoformat(max(days))
    return None


def backfill(now=None):
    """Fetch archived prices from the last archived slot (or ARCHIVE_DAYS ago) up to now."""
    now = now or datetime.now().astimezone()
    start = _archive_latest() or (now - timedelta(days=ARCHIVE_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    while start < now - timedelta(minutes=15):
        cursor = base64.b64encode(start.isoformat().encode()).decode()
        nodes = _query(RANGE_QUERY % (672, cursor))["priceInfoRange"]["nodes"]   # 7 days per page (API max)
        nodes = [x for x in nodes if datetime.fromisoformat(x["startsAt"]) >= start]
        if not nodes:
            break
        archive(nodes)
        n += len(nodes)
        nxt = max(datetime.fromisoformat(x["startsAt"]) for x in nodes) + timedelta(minutes=15)
        if nxt <= start:
            break
        start = nxt
        time.sleep(1)                                 # be gentle with the API
    if n:
        print(f"tibber: archived {n} historic prices", flush=True)


def _worker(keep_archive):
    _load_cache()
    if keep_archive:
        try:
            _archive_upgrade()
            backfill()
            with open(CACHE) as f:
                archive(json.load(f))                 # today + tomorrow from the cache
        except Exception as e:
            print(f"tibber: backfill failed: {e}", flush=True)
    while True:
        now = datetime.now().astimezone()
        if _needs_refresh(now):
            try:
                raw = _fetch()
                _store(raw)
                _save_cache(raw)
                if keep_archive:
                    archive(raw)
            except Exception as e:
                print(f"tibber: fetch failed: {e}", flush=True)
                time.sleep(300)
                continue
        time.sleep(60)


def start(archive=False):
    threading.Thread(target=_worker, args=(archive,), daemon=True, name="tibber").start()


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
