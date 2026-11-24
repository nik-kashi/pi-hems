import itertools
# Simulates the zero-export discharge loop against device ramp rate and P1
# delay, sweeping settle time and gain. Used to pick WRITE_MIN_INTERVAL_S = 6.
def run(ramp, skew, settle, gain, target=10, dead=15):
    load = ([600]*15 + [560]*10 + [800]*15 + [550]*20 + [1200]*15 + [300]*15 + [700]*30) * 3
    out_cmd, actual, last = 0.0, 0.0, -99
    hist = [0.0]*(skew+1)
    exp = imp = 0; writes = 0
    for i, L in enumerate(load):
        t = i * 2
        # device ramps towards the command at `ramp` W per second
        step = ramp * 2
        actual += max(-step, min(step, out_cmd - actual))
        hist = hist[1:] + [actual]
        grid = L - hist[0]                 # P1 reports `skew` steps late
        true = L - actual
        if true < 0: exp += -true*2/3600
        else: imp += max(0, true - target)*2/3600
        house = grid + hist[0]             # consistent pair would use the same moment
        house = grid + actual              # what the executor really does
        want = max(0, min(1600, house - target))
        tgt = out_cmd + gain * (want - out_cmd)
        if abs(tgt - out_cmd) >= dead and t - last >= settle:
            out_cmd, last = tgt, t; writes += 1
    return exp, imp, writes
cases = [(300, 0), (100, 1), (100, 2), (60, 2)]   # (ramp W/s, P1 late steps of 2 s)
print("settle gain | " + " | ".join(f"ramp{r} P1+{s*2}s exp/imp/wr" for r, s in cases))
for settle, gain in itertools.product((2, 4, 6, 8), (1.0, 0.6)):
    res = [run(r, s, settle, gain) for r, s in cases]
    tot = sum(e + i for e, i, _ in res)
    print(f"{settle:>4}s {gain:>4} | " + " | ".join(f"{e:5.1f}/{i:5.1f}/{w:3}" for e, i, w in res) + f"  | total {tot:6.1f} Wh")
