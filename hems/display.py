#!/usr/bin/env python3
"""HEMS status screen for the 3.5in SPI TFT (/dev/fb1, 480x320 RGB565 landscape panel).

Drawn on a 320x480 portrait canvas, rotated 90 deg clockwise onto the panel.
Dims automatically between sunset and sunrise.
"""
import math, os, time
from datetime import datetime, timedelta, timezone
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import json
import devices
import tibber

# ---- settings -------------------------------------------------------------
FBDEV = "/dev/fb1"
from config import LAT, LON
NIGHT_LEVEL = 0.25            # brightness factor at night (0..1)
REFRESH_S = 2
ROTATE = Image.Transpose.ROTATE_270   # = 90 deg clockwise; use ROTATE_90 to flip
# ---------------------------------------------------------------------------

W, H = 320, 480
FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
f_big, f_mid, f_sm, f_xs = (ImageFont.truetype(FB, 34), ImageFont.truetype(FB, 20),
                           ImageFont.truetype(FR, 15), ImageFont.truetype(FR, 12))
BG, TILE, FG, MUTED = (12, 16, 24), (26, 32, 46), (240, 240, 240), (140, 150, 170)
BLUE, YELLOW, GREEN, PURPLE = (90, 160, 255), (255, 200, 60), (80, 210, 120), (220, 120, 220)


def sun_times(day: datetime, lat=LAT, lon=LON):
    """Sunrise/sunset in UTC hours (NOAA approximation, good to a few minutes)."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) - math.sin(la) * math.sin(decl)) / (math.cos(la) * math.cos(decl))
    ha = math.degrees(math.acos(max(-1, min(1, cos_ha))))
    rise = (720 - 4 * (lon + ha) - eqt) / 60
    sett = (720 - 4 * (lon - ha) - eqt) / 60
    return rise, sett


def is_night(now_utc: datetime) -> bool:
    rise, sett = sun_times(now_utc)
    h = now_utc.hour + now_utc.minute / 60
    return not (rise <= h <= sett)


class Res:
    """Tiny CPU / RAM / temperature sampler."""
    def __init__(self):
        self.prev = self._cpu()

    @staticmethod
    def _cpu():
        v = list(map(int, open("/proc/stat").readline().split()[1:]))
        idle = v[3] + v[4]
        return idle, sum(v)

    def sample(self):
        idle, total = self._cpu()
        pi, pt = self.prev
        self.prev = (idle, total)
        cpu = 100 * (1 - (idle - pi) / max(1, total - pt))
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        used = mem["MemTotal"] - mem["MemAvailable"]
        ram = 100 * used / mem["MemTotal"]
        temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        return cpu, ram, used // 1024, mem["MemTotal"] // 1024, temp


def bar(d, x, y, w, h, pct, col):
    d.rounded_rectangle((x, y, x + w, y + h), 4, fill=(40, 46, 60))
    fw = int(w * max(0, min(100, pct)) / 100)
    if fw > 6:
        d.rounded_rectangle((x, y, x + fw, y + h), 4, fill=col)


STALE_S = 15
_last = {"p1": None, "sf": None}


def poll():
    for key, fn in (("p1", devices.read_p1), ("sf", devices.read_solarflow)):
        try:
            _last[key] = fn()
        except Exception:
            pass
    return _last


def _fresh(x):
    return x is not None and time.time() - x["ts"] < STALE_S


def tiles(v):
    p1, sf = v.get("p1"), v.get("sf")
    out = []
    if p1:
        g = p1["grid_w"]
        sub = "L1 {} / L2 {} / L3 {} W".format(*p1["phases_w"])
        out.append(("Grid  " + ("import" if g >= 0 else "export"), f"{abs(g)} W", sub, BLUE, _fresh(p1)))
    else:
        out.append(("Grid", "-- W", "", BLUE, False))
    if sf:
        out.append(("Solar", f"{sf['solar_w']} W", "", YELLOW, _fresh(sf)))
        if sf["bat_charge_w"] > 0:
            sub = f"charging {sf['bat_charge_w']} W"
            if sf["grid_to_bat_w"] > 0:
                sub += f"  (grid {sf['grid_to_bat_w']} W)"
        elif sf["bat_discharge_w"] > 0:
            sub = f"discharging {sf['bat_discharge_w']} W"
        else:
            sub = "idle"
        out.append(("Battery", f"{sf['soc']} %", sub, GREEN, _fresh(sf)))
    else:
        out.append(("Solar", "-- W", "", YELLOW, False))
        out.append(("Battery", "-- %", "", GREEN, False))
    cur = tibber.current()
    if cur:
        price, level = cur
        out.append(("Price  ct/kWh", f"{price * 100:.0f}", level.replace("_", " ").lower(), PURPLE, True))
    else:
        out.append(("Price  ct/kWh", "--", "no price data", PURPLE, False))
    return out


MODE_COL = {"CHARGE": (80, 210, 120), "DISCHARGE": (255, 150, 60), "BYPASS": (150, 160, 180)}
PLAN_FILE = "/run/hems/plan.json"


def load_plan():
    try:
        with open(PLAN_FILE) as f:
            p = json.load(f)
        if time.time() - datetime.fromisoformat(p["created"]).timestamp() > 20 * 60:
            return None                     # stale
        return p
    except Exception:
        return None


def plan_box(d, y):
    p = load_plan()
    d.rounded_rectangle((12, y, W - 12, y + 42), 10, fill=TILE)
    if not p:
        d.text((24, y + 13), "Plan: not available", font=f_sm, fill=MUTED)
        return
    s0 = p["slots"][0]
    col = MODE_COL[s0["mode"]]
    d.rounded_rectangle((12, y, W - 12, y + 42), 10, fill=TILE, outline=col, width=2)
    txt = s0["mode"] + (f" {s0['power_w']} W" if s0["power_w"] else "")
    d.text((24, y + 5), txt, font=f_sm, fill=col)
    if p.get("shadow"):
        d.text((W - 24, y + 7), "shadow", font=f_xs, fill=MUTED, anchor="ra")
    nxt = None
    for sl in p["slots"][1:]:
        if sl["mode"] != s0["mode"]:
            nxt = sl
            break
    line = f"€{p['grid_saving_eur'] - p['wear_eur']:.2f} net"
    if nxt:
        t = datetime.fromisoformat(nxt["start"])
        day = "" if t.date() == datetime.now().date() else t.strftime("%a ")
        line = f"next {nxt['mode'].lower()} {day}{t:%H:%M}  ·  " + line
    d.text((24, y + 25), line, font=f_xs, fill=MUTED)


def price_chart(d, x, y, h):
    """15-minute prices for today, plus tomorrow once published.
    Today only: 2 px per slot. Today + tomorrow: 1 px per slot with a midnight
    marker. Past slots dark, current slot white, future slots coloured by plan."""
    now = datetime.now().astimezone()
    days = [now.date(), now.date() + timedelta(days=1)]
    slots = [(t, p) for t, p, _ in tibber.slots() if t.date() in days]
    if not slots:
        return
    two_days = any(t.date() == days[1] for t, _ in slots)
    px = 1 if two_days else 2
    vals = [p for _, p in slots]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 1e-6)
    plan_modes = {sl["start"]: sl["mode"] for sl in (load_plan() or {}).get("slots", [])}
    if two_days:
        d.line((x + 96, y - 2, x + 96, y + h), fill=(70, 76, 90))
    for t, p in slots:
        i = (t.date() - days[0]).days * 96 + t.hour * 4 + t.minute // 15
        bh = 3 + (h - 3) * (p - lo) / span
        x0 = x + px * i
        end = t + timedelta(minutes=15)
        if t <= now < end:
            col = (255, 235, 255)
        elif end <= now:
            col = (75, 50, 80)
        else:
            mode = plan_modes.get(t.isoformat())
            col = tuple(int(c * 0.8) for c in MODE_COL[mode]) if mode in ("CHARGE", "DISCHARGE") else (120, 70, 125)
        d.rectangle((x0, round(y + h - bh), x0 + px - 1, y + h), fill=col)


def render(values, res, now):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((12, 10), "HEMS", font=f_mid, fill=FG)
    d.text((W - 12, 13), now.strftime("%a %H:%M"), font=f_sm, fill=MUTED, anchor="ra")

    y, th = 44, 76
    for label, val, sub, col, ok in tiles(values):
        d.rounded_rectangle((12, y, W - 12, y + th), 12, fill=TILE,
                            outline=col if ok else (70, 76, 90), width=2)
        d.text((26, y + 10), label, font=f_sm, fill=col if ok else MUTED)
        if not ok:
            d.text((W - 26, y + 12), "offline", font=f_xs, fill=(255, 120, 100), anchor="ra")
        d.text((W - 26, y + th - 30), val, font=f_big if len(val) < 9 else f_mid,
               fill=FG if ok else MUTED, anchor="rs")
        if sub:
            d.text((26, y + th - 12), sub, font=f_xs, fill=MUTED, anchor="ls")
        if label.startswith("Price") and ok:
            price_chart(d, 26, y + 30, 22)
        y += th + 6

    plan_box(d, y)

    # resource monitor strip
    cpu, ram, used, total, temp = res
    y = H - 50
    d.line((12, y - 8, W - 12, y - 8), fill=(40, 46, 60))
    d.text((12, y), "CPU", font=f_xs, fill=MUTED)
    bar(d, 46, y + 2, 90, 10, cpu, (120, 180, 255))
    d.text((142, y), f"{cpu:3.0f}%", font=f_xs, fill=FG)
    d.text((186, y), "RAM", font=f_xs, fill=MUTED)
    bar(d, 220, y + 2, 60, 10, ram, (150, 220, 160))
    d.text((W - 12, y), f"{ram:2.0f}%", font=f_xs, fill=FG, anchor="ra")
    d.text((12, y + 20), f"{used}/{total} MB", font=f_xs, fill=MUTED)
    tcol = (255, 120, 100) if temp >= 70 else MUTED
    d.text((W - 12, y + 20), f"{temp:.0f}°C", font=f_xs, fill=tcol, anchor="ra")
    return img


def to_fb(img, level):
    img = img.transpose(ROTATE)
    a = np.asarray(img, dtype=np.float32) * level
    a = a.astype(np.uint16)
    rgb565 = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    with open(FBDEV, "wb") as fb:
        fb.write(rgb565.astype("<u2").tobytes())


def main():
    tibber.start()
    r = Res()
    time.sleep(0.5)
    while True:
        now = datetime.now().astimezone()
        level = NIGHT_LEVEL if is_night(now.astimezone(timezone.utc)) else 1.0
        if os.environ.get("HEMS_FORCE_LEVEL"):
            level = float(os.environ["HEMS_FORCE_LEVEL"])
        to_fb(render(poll(), r.sample(), now), level)
        time.sleep(REFRESH_S)


if __name__ == "__main__":
    main()
