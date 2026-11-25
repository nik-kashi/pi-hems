"""Logger: SIGTERM/kill -9 mid-interval must lose nothing; old CSVs gain columns."""
import csv, glob, os, signal, subprocess, sys, tempfile, time
HEMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../hems")
tmp = tempfile.mkdtemp()
CODE = f'''
import sys, time; sys.path.insert(0, {HEMS!r})
import logger as L
L.DATA_DIR, L.RAM_DIR = {tmp!r}+"/d", {tmp!r}+"/r"
L.RAM_STATE, L.DISK_STATE = L.RAM_DIR+"/s.json", L.DATA_DIR+"/s.json"
L.SAMPLE_S = 0.5
L.slot_of = lambda t: t.replace(second=t.second - t.second % 20, microsecond=0)     # 20 s "intervals"
L.sample = lambda: dict(ac_in=1000, ac_out=0, solar=0, dc=900.0, soc=50, rep_to_pack=0, rep_from_pack=0,
    batcur="", pack_soc="", ac_mode=1, grid=6500, l1=100, l2=400, l3=6000)
L.main()'''
old = ["start", "end", "soc_start", "soc_end", "ac_in_wh", "covered_s"]
os.makedirs(tmp + "/d"); f = f"{tmp}/d/energy-{time.strftime('%Y-%m')}.csv"
open(f, "w").write(",".join(old) + "\nX,Y,1,2,3.5,900\n")
while time.time() % 20 > 1: time.sleep(0.1)
t0 = time.time()
for sig in (signal.SIGTERM, signal.SIGKILL, signal.SIGTERM):
    p = subprocess.Popen([sys.executable, "-c", CODE], stdout=subprocess.PIPE, text=True); time.sleep(6); p.send_signal(sig); p.wait()
p = subprocess.Popen([sys.executable, "-c", CODE], stdout=subprocess.PIPE, text=True); time.sleep(6); p.terminate(); p.wait()
rows = list(csv.DictReader(open(f))); r = rows[1] if len(rows) > 1 else {}
print("header ok:", "l3_over_s" in rows[0], "| old row kept:", rows[0]["ac_in_wh"] == "3.5" and rows[0]["l1_wh"] == "")
print("interval after 3 stops:", {k: r.get(k) for k in ("covered_s", "ac_in_wh", "l3_wh", "l3_max_w", "l3_over_s", "grid_import_wh")})
c = float(r.get("covered_s") or 0)
ok = "l3_over_s" in rows[0] and rows[0]["ac_in_wh"] == "3.5" and c >= 17 and abs(float(r["ac_in_wh"]) - 1000 * c / 3600) < 0.4 and r["l3_max_w"] == "6000"
print("PASS" if ok else "FAIL"); sys.exit(not ok)
