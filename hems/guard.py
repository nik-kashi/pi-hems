"""Fuse guard: keeps grid charging within the spare capacity of the battery's phase.

other   = phase power - battery AC input + battery AC output   (everything else on it)
allowed = safe limit - highest `other` of the last GUARD_WINDOW_S seconds

The window maximum matters because the P1 reading lags the battery: while
charging ramps up, the late P1 makes `other` look too small, so one reading
alone would allow too much. Cuts are immediate, raises are rate limited, and
without recent P1 data charging is not allowed at all (fail safe).
"""
from collections import deque

import config as C


class ChargeGuard:
    def __init__(self):
        self.hist = deque()        # (t, other_w)
        self.last_good = None      # time of the last new P1 message
        self.last_msg = None
        self.limit = 0
        self.last_change = -1e9

    def observe(self, now, phase_w, msg, ac_in, ac_out):
        """Feed one reading; phase_w=None when the P1 could not be read."""
        if phase_w is None or (msg is not None and msg == self.last_msg):
            return                                  # no reading, or a frozen meter
        if self.last_good is not None and now - self.last_good > C.P1_STALE_S:
            self.hist.clear()                       # gap: collect fresh data first
        self.last_good, self.last_msg = now, msg
        self.hist.append((now, max(0, phase_w - ac_in + ac_out)))
        while self.hist and now - self.hist[0][0] > C.GUARD_WINDOW_S:
            self.hist.popleft()

    def allow(self, now, requested):
        """(charge limit in W, reason) for a requested charge power."""
        if self.last_good is None or now - self.last_good > C.P1_STALE_S:
            self.limit, self.last_change = 0, now
            return 0, "no P1 data -> no grid charging"
        if now - self.hist[0][0] < C.GUARD_MIN_DATA_S:
            self.limit = 0
            return 0, "guard collecting data"
        other = max(o for _, o in self.hist)
        spare = C.SAFE_PHASE_W - other
        target = max(0, min(requested, int(spare)))
        if target < C.MIN_ACTION_W:
            target = 0
        if target < self.limit:
            self.limit, self.last_change = target, now
        elif target > self.limit and (now - self.last_change >= C.GUARD_RAISE_S or self.limit == 0):
            if target == requested or target - self.limit >= C.MIN_ACTION_W:
                self.limit, self.last_change = target, now
        why = "plan" if self.limit >= requested else \
              f"L{C.BATTERY_PHASE + 1} guard: other load {other:.0f} W, limit {C.SAFE_PHASE_W} W"
        return int(self.limit), why
