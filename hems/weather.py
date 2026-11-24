"""Current weather from Open-Meteo (free, no API key), refreshed every 15 min.

Only the approximate location from config.py is sent. Kept in memory only.
"""
import json
import threading
import time
import urllib.request

from config import LAT, LON

URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
       "&current=temperature_2m,weather_code,is_day&timezone=auto")
REFRESH_S = 15 * 60
RETRY_S = 5 * 60

_lock = threading.Lock()
_current = None      # {"temp_c", "code", "is_day", "ts"}


def _fetch():
    req = urllib.request.Request(URL.format(lat=LAT, lon=LON), headers={"User-Agent": "hems-pi/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        c = json.load(r)["current"]
    return {"temp_c": c["temperature_2m"], "code": c["weather_code"],
            "is_day": bool(c["is_day"]), "ts": time.time()}


def _worker():
    global _current
    while True:
        try:
            w = _fetch()
            with _lock:
                _current = w
            time.sleep(REFRESH_S)
        except Exception as e:
            print(f"weather: fetch failed: {e}", flush=True)
            time.sleep(RETRY_S)


def start():
    threading.Thread(target=_worker, daemon=True, name="weather").start()


def current():
    """Latest reading, or None if missing / older than 1 hour."""
    with _lock:
        w = _current
    if w and time.time() - w["ts"] < 3600:
        return w
    return None


def kind(code):
    """WMO weather code -> icon kind."""
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "thunder"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    return "cloudy"
