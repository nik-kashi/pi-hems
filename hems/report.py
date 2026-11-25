#!/usr/bin/env python3
"""Summarise /var/lib/hems/energy-*.csv into measured efficiencies.

Usage: report.py [YYYY-MM-DD ...]   (default: all data)
"""
import csv
import glob
import sys

days = set(sys.argv[1:])
tot = {}
rows = 0
for path in sorted(glob.glob("/var/lib/hems/energy-*.csv")):
    for r in csv.DictReader(open(path)):
        if days and r["start"][:10] not in days:
            continue
        rows += 1
        for k, v in r.items():
            if k.endswith("_wh") or k.endswith("_s"):
                tot[k] = tot.get(k, 0) + float(v or 0)
            elif k.endswith("_max_w"):
                tot[k] = max(tot.get(k, 0), float(v or 0))

if not rows:
    sys.exit("no data yet")

g = lambda k: tot.get(k, 0.0)
hours = g("covered_s") / 3600
print(f"intervals: {rows} ({hours:.1f} h covered)\n")
print(f"grid import {g('grid_import_wh')/1000:7.2f} kWh   grid export {g('grid_export_wh')/1000:6.2f} kWh")
print(f"solar       {g('solar_wh')/1000:7.2f} kWh")
print(f"AC in       {g('ac_in_wh')/1000:7.2f} kWh   AC out      {g('ac_out_wh')/1000:6.2f} kWh")
print(f"cells in    {g('dc_in_wh')/1000:7.2f} kWh   cells out   {g('dc_out_wh')/1000:6.2f} kWh\n")


def eff(name, num, den, unit=""):
    if den < 50:
        print(f"{name:<30} not enough data ({den:.0f} Wh)")
    else:
        print(f"{name:<30} {100*num/den:5.1f} %   ({num/1000:.2f} of {den/1000:.2f} kWh)")


eff("grid AC -> cells (charge)", g("gc_dc_in_wh"), g("gc_ac_in_wh"))
eff("solar   -> cells (charge)", g("sc_dc_in_wh"), g("sc_solar_wh"))
eff("cells   -> AC home (discharge)", g("dc_ac_out_wh"), g("dc_dc_out_wh"))
if g("gc_ac_in_wh") >= 50 and g("dc_dc_out_wh") >= 50:
    rt = (g("gc_dc_in_wh") / g("gc_ac_in_wh")) * (g("dc_ac_out_wh") / g("dc_dc_out_wh"))
    print(f"{'=> round trip grid->grid':<30} {100*rt:5.1f} %   (loss {100*(1-rt):.1f} %)")
if g("idle_s") > 1800:
    standby = (g("idle_dc_out_wh") - g("idle_dc_in_wh")) / (g("idle_s") / 3600)
    print(f"{'standby drain while idle':<30} {standby:5.1f} W   ({g('idle_s')/3600:.1f} h idle)")
if any(g(f"l{n}_max_w") for n in (1, 2, 3)):
    print("\nphases        " + "".join(f"{'L' + str(n):>10}" for n in (1, 2, 3)))
    print("net energy    " + "".join(f"{g(f'l{n}_wh') / 1000:10.2f}" for n in (1, 2, 3)) + " kWh")
    print("highest       " + "".join(f"{g(f'l{n}_max_w') / 230:10.1f}" for n in (1, 2, 3)) + " A (at 230 V)")
    print("above 25 A    " + "".join(f"{g(f'l{n}_over_s'):10.0f}" for n in (1, 2, 3)) + " s")
