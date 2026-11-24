#!/usr/bin/env python3
"""Watch / poke the SolarFlow from the command line (run on the Pi in /opt/hems).

  zcheck.py watch SECONDS [LABEL]      print control state every 3 s
  zcheck.py write '{"outputLimit":0}'  write properties (sn added automatically)
"""
import json
import sys
import time

sys.path.insert(0, "/opt/hems")
import devices  # noqa: E402


def line(label):
    p = devices.solarflow_report()["properties"]
    try:
        g = devices.read_p1()["grid_w"]
    except Exception:
        g = None
    print(f"{label} {time.strftime('%H:%M:%S')} grid={g!s:>5} acMode={p['acMode']} "
          f"in={p['inputLimit']:4} out={p['outputLimit']:4} smart={p.get('smartMode')} "
          f"gridIn={p['gridInputPower']:4} homeOut={p['outputHomePower']:4} "
          f"solar={p['solarInputPower']:4} soc={p['electricLevel']}", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "watch":
        end = time.time() + float(sys.argv[2])
        label = sys.argv[3] if len(sys.argv) > 3 else ""
        while time.time() < end:
            line(label)
            time.sleep(3)
    elif cmd == "write":
        sn = devices.solarflow_report()["sn"]
        props = json.loads(sys.argv[2])
        print("write", props, "->", devices.write_solarflow(sn, props), flush=True)
