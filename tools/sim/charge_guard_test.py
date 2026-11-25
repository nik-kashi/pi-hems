"""Charge guard vs the old phase check: late P1, slow device ramp, load steps."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../hems"))
import config as C
from guard import ChargeGuard

PREV = None
if len(sys.argv) > 1:                              # optional: folder with guard_old.py to compare
    sys.path.insert(0, sys.argv[1]); import guard_old; PREV = guard_old.ChargeGuard

def run(kind, loads, req=2000, skew=2, ramp=100, p1_fail=None):
    g, ac, cmd, over, worst, writes, wh = (PREV if kind == 'prev' else ChargeGuard)(), 0.0, 0, 0, 0, 0, 0.0
    hist = [loads[0]] * (skew + 1); tail = []
    for i, other in enumerate(loads):
        t = i * 2
        ac += max(-ramp * 2, min(ramp * 2, cmd - ac))
        true = other + ac
        hist = hist[1:] + [true]
        fail = bool(p1_fail and p1_fail[0] <= t < p1_fail[1])
        if kind in ("new", "prev"):
            g.observe(t, None if fail else hist[0], i, ac, 0); new, _ = g.allow(t, req)
        else:
            new = req if fail else max(0, req - max(0, int(hist[0] - 5500)))
        writes += new != cmd; cmd = new
        over += 2 if true > C.FUSE_A * C.LINE_V else 0
        worst = max(worst, true); wh += ac * 2 / 3600; tail.append(cmd)
    return dict(over_s=over, worst_w=round(worst), writes=writes, wh=round(wh), last=tail[-1], tail=tail)

S = lambda *seg: [w for w, s in seg for _ in range(s // 2)]
cases = {"normal 800 W": (S((800, 600)), {}),
         "steady 4500 W": (S((800, 60), (4500, 600)), {}),
         "steps 1000/4200": (S(*[(1000, 120), (4200, 120)] * 5), {}),
         "P1 lost, load 4500 W": (S((800, 100), (4500, 300)), {"p1_fail": (90, 400)}),
         "skew 4 s+, slow ramp": (S((800, 60), (4000, 400)), {"skew": 4, "ramp": 60})}
ok = True
import random
def noisy(seed, secs=3600):
    r, out, base = random.Random(seed), [], 4300
    for i in range(secs // 2):
        if i % 150 == 149: base = r.choice([3900, 4300, 4600])     # EV / heat pump changing
        spike = 1300 if i % 200 in (100, 101, 102) else 0
        out.append(int(base + r.gauss(0, 40) + spike))
    return out
def utilisation(kind, loads, **kw):
    r = run(kind, loads, **kw); ideal = sum(min(2000, max(0, C.SAFE_PHASE_W - o)) for o in loads) * 2 / 3600
    return r, 100 * r["wh"] / ideal
for seed in (1, 2, 3):
    for skew in (1, 2):
        kinds = ["new"] + (["prev"] if PREV else [])
        for kind in kinds:
            r, u = utilisation(kind, noisy(seed), skew=skew)
            print(f"EV noise seed {seed} P1+{skew*2}s {kind:4}: used {u:4.0f}% of spare capacity, over 25A {r['over_s']:3}s, writes {r['writes']}")
        ok &= utilisation("new", noisy(seed), skew=skew)[1] > 60
r = run("new", S((800, 60), (4500, 300), (800, 120))); ok &= r["last"] == 2000     # must scale back up
for name, (l, kw) in cases.items():
    for kind in ("old", "new"):
        r = run(kind, l, **kw); print(f"{name:22} {kind}: over 25A {r['over_s']:4}s worst {r['worst_w']:5} W writes {r['writes']:3} charged {r['wh']:4} Wh final {r['last']}")
r = run("new", *[cases["normal 800 W"][0]]); ok &= r["last"] == 2000
r = run("new", cases["steady 4500 W"][0]); ok &= r["last"] == C.SAFE_PHASE_W - 4500
r = run("new", cases["P1 lost, load 4500 W"][0], p1_fail=(90, 400)); ok &= r["last"] == 0
g = ChargeGuard()                                  # frozen meter: same messageId
for t in range(0, 60, 2): g.observe(t, 800, 7, 0, 0)
ok &= g.allow(60, 2000)[0] == 0
print("ASSERTIONS", "PASS" if ok else "FAIL"); sys.exit(not ok)
