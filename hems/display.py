#!/usr/bin/env python3
"""HEMS status screen for the 3.5in SPI TFT (/dev/fb1, 480x320 RGB565 landscape panel).

Drawn on a 320x480 portrait canvas, rotated 90 deg clockwise onto the panel.
Dims automatically between sunset and sunrise.

Layout (design "A", see docs/screenshot.png):
  header      HEMS · time (centre) · weather (right)
  2x2 tiles   Solar | Grid / Battery | Home, with energy-flow arrows between them
  price       current price, today+tomorrow 15-min chart coloured by plan, legend
  plan        current mode, next change, expected saving
  resources   CPU (+temp) / RAM / Disk bars
"""
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import devices
import tibber
import weather
from config import LAT, LON

# ---- settings -------------------------------------------------------------
FBDEV = "/dev/fb1"
NIGHT_LEVEL = 0.25            # brightness factor at night (0..1)
REFRESH_S = 2
ROTATE = Image.Transpose.ROTATE_270   # = 90 deg clockwise; use ROTATE_90 to flip
STALE_S = 15
FLOW_MIN_W = 15               # flows below this are drawn as idle
PLAN_FILE = "/run/hems/plan.json"
# ---------------------------------------------------------------------------

W, H = 320, 480
FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_f = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _f:
        _f[key] = ImageFont.truetype(FB if bold else FR, size)
    return _f[key]


BG, TILE, FG, MUTED, LINE = (12, 16, 24), (26, 32, 46), (240, 240, 240), (140, 150, 170), (38, 45, 61)
YELLOW, BLUE, GREEN, WHITE, PURPLE = (255, 200, 60), (90, 160, 255), (80, 210, 120), (223, 228, 238), (220, 120, 220)
RED = (255, 120, 100)
MODE_COL = {"CHARGE": (80, 210, 120), "DISCHARGE": (255, 150, 60), "BYPASS": (150, 160, 180)}
CHART_COL = {"CHARGE": (64, 168, 96), "DISCHARGE": (204, 120, 48), "BYPASS": (120, 70, 125),
             "past": (75, 50, 80), "now": (255, 235, 255)}

# tile boxes (x, y) top-left, 116 x 92
TW, TH = 116, 92
POS = {"solar": (12, 44), "grid": (192, 44), "battery": (12, 180), "home": (192, 180)}


# ---- sun / night ------------------------------------------------------------
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
    return (720 - 4 * (lon + ha) - eqt) / 60, (720 - 4 * (lon - ha) - eqt) / 60


def is_night(now_utc: datetime) -> bool:
    rise, sett = sun_times(now_utc)
    h = now_utc.hour + now_utc.minute / 60
    return not (rise <= h <= sett)


# ---- resources --------------------------------------------------------------
class Res:
    """Tiny CPU / RAM / disk / temperature sampler."""
    def __init__(self):
        self.prev = self._cpu()

    @staticmethod
    def _cpu():
        v = list(map(int, open("/proc/stat").readline().split()[1:]))
        return v[3] + v[4], sum(v)

    def sample(self):
        idle, total = self._cpu()
        pi, pt = self.prev
        self.prev = (idle, total)
        cpu = 100 * (1 - (idle - pi) / max(1, total - pt))
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        ram = 100 * (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"]
        st = os.statvfs("/")
        disk = 100 * (1 - st.f_bavail / st.f_blocks)
        temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        return {"cpu": cpu, "ram": ram, "disk": disk, "temp": temp}


# ---- data -------------------------------------------------------------------
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


def flows(p1, sf):
    """Energy flows in W between the four tiles (all >= 0)."""
    f = dict.fromkeys(["solar_grid", "solar_battery", "solar_home",
                       "grid_battery", "grid_home", "battery_home"], 0)
    if not sf:
        return f, None
    solar, ac_in, ac_out = sf["solar_w"], sf["grid_to_bat_w"], sf["to_home_w"]
    solar_out = min(solar, ac_out)                 # solar sent out on the AC side
    f["solar_battery"] = max(0, solar - solar_out)
    f["battery_home"] = max(0, ac_out - solar_out)
    f["grid_battery"] = ac_in
    home = None
    if p1:
        g = p1["grid_w"]
        home = max(0, g + ac_out - ac_in)
        if g >= 0:
            f["grid_home"] = max(0, g - ac_in)
            f["solar_home"] = solar_out
        else:                                      # exporting
            f["solar_grid"] = min(solar_out, -g)
            f["solar_home"] = max(0, solar_out - f["solar_grid"])
    return f, home


def load_plan():
    try:
        with open(PLAN_FILE) as fh:
            p = json.load(fh)
        if time.time() - datetime.fromisoformat(p["created"]).timestamp() > 20 * 60:
            return None
        return p
    except Exception:
        return None


# ---- drawing helpers --------------------------------------------------------
def text_w(d, s, f):
    return int(d.textlength(s, font=f))


def bar(d, x, y, w, h, pct, col):
    d.rounded_rectangle((x, y, x + w, y + h), 4, fill=(40, 46, 60))
    fw = int(w * max(0, min(100, pct)) / 100)
    if fw >= 6:
        d.rounded_rectangle((x, y, x + fw, y + h), 4, fill=col)


def arrow(d, x1, y1, x2, y2, col, active):
    if not active:
        d.line((x1, y1, x2, y2), fill=LINE, width=2)
        return
    ang = math.atan2(y2 - y1, x2 - x1)
    hx, hy = x2 - 7 * math.cos(ang), y2 - 7 * math.sin(ang)
    d.line((x1, y1, hx, hy), fill=col, width=3)
    left = (hx + 5 * math.cos(ang + math.pi / 2), hy + 5 * math.sin(ang + math.pi / 2))
    right = (hx + 5 * math.cos(ang - math.pi / 2), hy + 5 * math.sin(ang - math.pi / 2))
    d.polygon([(x2, y2), left, right], fill=col)


def pill(d, cx, cy, txt, col):
    f = font(10, True)
    w = text_w(d, txt, f) + 10
    d.rounded_rectangle((cx - w / 2, cy - 7, cx + w / 2, cy + 7), 7, fill=BG, outline=col, width=1)
    d.text((cx, cy), txt, font=f, fill=col, anchor="mm")


# ---- icons (line drawings, ~14 px unless noted) ------------------------------
def icon_sun(d, cx, cy, col, r=3.5, ray=6.5):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=col, width=2)
    for k in range(8):
        a = k * math.pi / 4
        d.line((cx + (r + 1.5) * math.cos(a), cy + (r + 1.5) * math.sin(a),
                cx + ray * math.cos(a), cy + ray * math.sin(a)), fill=col, width=2)


def icon_grid(d, x, y, col):
    d.line((x + 3, y + 14, x + 7, y), fill=col, width=2)
    d.line((x + 11, y + 14, x + 7, y), fill=col, width=2)
    d.line((x + 1, y + 4, x + 13, y + 4), fill=col, width=2)
    d.line((x + 4, y + 9, x + 10, y + 9), fill=col, width=2)


def icon_battery(d, x, y, col):
    d.rounded_rectangle((x, y + 3, x + 12, y + 11), 2, outline=col, width=2)
    d.rectangle((x + 13, y + 6, x + 14, y + 8), fill=col)
    d.rectangle((x + 3, y + 6, x + 6, y + 8), fill=col)


def icon_home(d, x, y, col):
    d.line((x, y + 7, x + 7, y + 1), fill=col, width=2)
    d.line((x + 7, y + 1, x + 14, y + 7), fill=col, width=2)
    d.rectangle((x + 2, y + 7, x + 12, y + 14), outline=col, width=2)
    d.rectangle((x + 6, y + 10, x + 8, y + 14), fill=col)


def icon_cloud(d, x, y, col, fill=TILE):
    """Cloud in a 22x14 box at (x, y)."""
    d.ellipse((x + 6, y, x + 17, y + 11), fill=fill, outline=col, width=2)
    d.ellipse((x, y + 4, x + 10, y + 14), fill=fill, outline=col, width=2)
    d.ellipse((x + 12, y + 4, x + 22, y + 14), fill=fill, outline=col, width=2)
    d.rectangle((x + 5, y + 7, x + 17, y + 12), fill=fill)
    d.line((x + 5, y + 13, x + 17, y + 13), fill=col, width=2)


def icon_moon(d, cx, cy, col, r=5):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
    d.ellipse((cx - r + 4, cy - r - 2, cx + r + 4, cy + r - 2), fill=BG)


def weather_icon(d, x, y, kind, day):
    """Weather icon in a 22x22 box at (x, y)."""
    sky = (lambda: icon_sun(d, x + 8, y + 8, YELLOW, r=3, ray=6)) if day else \
          (lambda: icon_moon(d, x + 8, y + 7, (180, 188, 208)))
    if kind == "clear":
        if day:
            icon_sun(d, x + 11, y + 11, YELLOW, r=4.5, ray=9)
        else:
            icon_moon(d, x + 11, y + 11, (180, 188, 208), r=7)
        return
    if kind == "partly":
        sky()
    icon_cloud(d, x, y + 6, WHITE, fill=BG)
    if kind == "rain":
        for k in range(3):
            d.line((x + 6 + 5 * k, y + 21, x + 4 + 5 * k, y + 24), fill=BLUE, width=2)
    elif kind == "snow":
        for k in range(3):
            d.ellipse((x + 4 + 6 * k, y + 21, x + 7 + 6 * k, y + 24), fill=WHITE)
    elif kind == "thunder":
        d.polygon([(x + 12, y + 17), (x + 8, y + 23), (x + 11, y + 23), (x + 9, y + 27),
                   (x + 15, y + 20), (x + 12, y + 20)], fill=YELLOW)
    elif kind == "fog":
        for k in range(2):
            d.line((x + 2, y + 22 + 3 * k, x + 20, y + 22 + 3 * k), fill=MUTED, width=2)


# ---- sections ---------------------------------------------------------------
def header(d, now):
    d.text((12, 8), "HEMS", font=font(20, True), fill=FG)
    d.text((W / 2, 12), now.strftime("%a %H:%M"), font=font(14), fill=MUTED, anchor="ma")
    w = weather.current()
    if w:
        t = f"{w['temp_c']:.0f}°C"
        f = font(15, True)
        tx = W - 12 - text_w(d, t, f)
        d.text((tx, 11), t, font=f, fill=FG)
        weather_icon(d, tx - 27, 5, weather.kind(w["code"]), w["is_day"])


def tile(d, key, label, icon, col, value, unit, sub, ok, sub_col=MUTED):
    x, y = POS[key]
    d.rounded_rectangle((x, y, x + TW, y + TH), 12, fill=TILE, outline=col if ok else (70, 76, 90), width=2)
    icon(d, x + 10, y + 10, col if ok else MUTED)
    d.text((x + 30, y + 9), label, font=font(13), fill=col if ok else MUTED)
    if not ok:
        d.text((x + TW - 10, y + 12), "offline", font=font(10), fill=RED, anchor="ra")
    fv, fu = font(26, True), font(14)
    uw = text_w(d, unit, fu)
    d.text((x + TW - 10, y + 60), unit, font=fu, fill=FG if ok else MUTED, anchor="rs")
    d.text((x + TW - 10 - uw, y + 60), value, font=fv, fill=FG if ok else MUTED, anchor="rs")
    if sub:
        d.text((x + 10, y + TH - 10), sub, font=font(11), fill=sub_col, anchor="ls")


def energy_section(d, v):
    p1, sf = v.get("p1"), v.get("sf")
    p1_ok, sf_ok = _fresh(p1), _fresh(sf)
    f, home = flows(p1 if p1_ok else None, sf if sf_ok else None)
    on = {k: w >= FLOW_MIN_W for k, w in f.items()}

    # arrows first (tiles are drawn on top of their ends)
    arrow(d, 132, 90, 188, 90, YELLOW, on["solar_grid"])
    arrow(d, 132, 140, 188, 176, YELLOW, on["solar_home"])
    arrow(d, 70, 139, 70, 175, YELLOW, on["solar_battery"])
    arrow(d, 188, 140, 133, 175, BLUE, on["grid_battery"])
    arrow(d, 250, 139, 250, 175, BLUE, on["grid_home"])
    arrow(d, 131, 226, 187, 226, GREEN, on["battery_home"])

    # tiles
    if sf:
        tile(d, "solar", "Solar", lambda dd, x, y, c: icon_sun(dd, x + 7, y + 7, c, r=3, ray=6.5),
             YELLOW, f"{sf['solar_w']}", " W", "" if sf["solar_w"] >= FLOW_MIN_W else "no sun", sf_ok)
        if sf["bat_charge_w"] > 0:
            bsub = f"charging {sf['bat_charge_w']} W"
        elif sf["bat_discharge_w"] > 0:
            bsub = f"discharging {sf['bat_discharge_w']} W"
        else:
            bsub = "idle"
        tile(d, "battery", "Battery", icon_battery, GREEN, f"{sf['soc']}", " %", bsub, sf_ok)
    else:
        tile(d, "solar", "Solar", lambda dd, x, y, c: icon_sun(dd, x + 7, y + 7, c, r=3, ray=6.5),
             YELLOW, "--", " W", "", False)
        tile(d, "battery", "Battery", icon_battery, GREEN, "--", " %", "", False)
    if p1:
        g = p1["grid_w"]
        tile(d, "grid", "Grid", icon_grid, BLUE, f"{abs(g)}", " W", "import" if g >= 0 else "export", p1_ok)
    else:
        tile(d, "grid", "Grid", icon_grid, BLUE, "--", " W", "", False)
    tile(d, "home", "Home", icon_home, WHITE, f"{home}" if home is not None else "--", " W",
         "consumption", home is not None)

    # flow labels on top
    if on["solar_grid"]:
        pill(d, 160, 90, f"{f['solar_grid']} W", YELLOW)
    if on["solar_battery"]:
        pill(d, 70, 157, f"{f['solar_battery']} W", YELLOW)
    if on["grid_home"]:
        pill(d, 250, 157, f"{f['grid_home']} W", BLUE)
    if on["battery_home"]:
        pill(d, 160, 226, f"{f['battery_home']} W", GREEN)
    both = on["grid_battery"] and on["solar_home"]
    if on["grid_battery"]:
        pill(d, 160, 150 if both else 158, f"{f['grid_battery']} W", BLUE)
    if on["solar_home"]:
        pill(d, 160, 167 if both else 158, f"{f['solar_home']} W", YELLOW)


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
            col = CHART_COL["now"]
        elif end <= now:
            col = CHART_COL["past"]
        else:
            col = CHART_COL.get(plan_modes.get(t.isoformat()), CHART_COL["BYPASS"])
        d.rectangle((x0, round(y + h - bh), x0 + px - 1, y + h), fill=col)


def price_section(d, y):
    cur = tibber.current()
    ok = cur is not None
    d.rounded_rectangle((12, y, W - 12, y + 76), 12, fill=TILE, outline=PURPLE if ok else (70, 76, 90), width=2)
    label = "Price ct/kWh"
    d.text((24, y + 8), label, font=font(13), fill=PURPLE if ok else MUTED)
    if not ok:
        d.text((W - 24, y + 12), "no price data", font=font(10), fill=RED, anchor="ra")
        d.text((W - 24, y + 50), "--", font=font(32, True), fill=MUTED, anchor="rs")
        return
    price, level = cur
    d.text((24 + text_w(d, label, font(13)) + 6, y + 10), "· " + level.replace("_", " ").lower(),
           font=font(11), fill=MUTED)
    d.text((W - 24, y + 50), f"{price * 100:.0f}", font=font(32, True), fill=FG, anchor="rs")
    price_chart(d, 24, y + 28, 22)
    # legend
    lx, ly = 24, y + 66
    for name, col in (("charge", CHART_COL["CHARGE"]), ("discharge", CHART_COL["DISCHARGE"]),
                      ("bypass", CHART_COL["BYPASS"]), ("now", CHART_COL["now"])):
        d.rectangle((lx, ly - 7, lx + 6, ly - 1), fill=col)
        d.text((lx + 10, ly), name, font=font(10), fill=MUTED, anchor="ls")
        lx += 10 + text_w(d, name, font(10)) + 9


def plan_section(d, y):
    p = load_plan()
    if not p:
        d.rounded_rectangle((12, y, W - 12, y + 42), 10, fill=TILE)
        d.text((24, y + 13), "Plan: not available", font=font(14), fill=MUTED)
        return
    s0 = p["slots"][0]
    col = MODE_COL[s0["mode"]]
    d.rounded_rectangle((12, y, W - 12, y + 42), 10, fill=TILE, outline=col, width=2)
    d.text((24, y + 5), s0["mode"] + (f" {s0['power_w']} W" if s0["power_w"] else ""), font=font(14), fill=col)
    if p.get("shadow"):
        d.text((W - 24, y + 7), "shadow", font=font(11), fill=MUTED, anchor="ra")
    nxt = next((sl for sl in p["slots"][1:] if sl["mode"] != s0["mode"]), None)
    line = f"€{p['grid_saving_eur'] - p['wear_eur']:.2f} net"
    if nxt:
        t = datetime.fromisoformat(nxt["start"])
        day = "" if t.date() == datetime.now().date() else t.strftime("%a ")
        line = f"next {nxt['mode'].lower()} {day}{t:%H:%M}  ·  " + line
    d.text((24, y + 24), line, font=font(11), fill=MUTED)


def resource_section(d, res, y):
    d.line((12, y, W - 12, y), fill=(40, 46, 60))
    cw, gap, f = 88, 16, font(11)
    items = [("CPU", res["cpu"], (120, 180, 255)), ("RAM", res["ram"], (150, 220, 160)),
             ("Disk", res["disk"], (230, 190, 120))]
    for k, (name, pct, col) in enumerate(items):
        x = 12 + k * (cw + gap)
        d.text((x, y + 12), name, font=f, fill=MUTED)
        val = f"{pct:.0f}%"
        if name == "CPU":
            tcol = RED if res["temp"] >= 70 else MUTED
            tt = f" · {res['temp']:.0f}°C"
            d.text((x + cw, y + 12), tt, font=f, fill=tcol, anchor="ra")
            d.text((x + cw - text_w(d, tt, f), y + 12), val, font=f, fill=FG, anchor="ra")
        else:
            d.text((x + cw, y + 12), val, font=f, fill=FG, anchor="ra")
        bar(d, x, y + 30, cw, 8, pct, col)


def render(values, res, now):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, now)
    energy_section(d, values)
    price_section(d, 280)
    plan_section(d, 362)
    resource_section(d, res, 414)
    return img


def to_fb(img, level):
    img = img.transpose(ROTATE)
    a = (np.asarray(img, dtype=np.float32) * level).astype(np.uint16)
    rgb565 = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    with open(FBDEV, "wb") as fb:
        fb.write(rgb565.astype("<u2").tobytes())


def main():
    tibber.start()
    weather.start()
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
