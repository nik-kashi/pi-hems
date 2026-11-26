"""Shared HEMS settings. All tunables live here."""

# --- devices (local network) ---------------------------------------------
SOLARFLOW = "http://192.168.2.32"    # starting point; devices.py rediscovers on change
P1_METER = "http://192.168.2.10"     # starting point; devices.py rediscovers on change

# --- location (approximate, for sunrise/sunset only) -------------------------
LAT, LON = 52.1, 5.1

# --- battery -----------------------------------------------------------------
CAPACITY_KWH = 8.16          # built-in 2.40 + 2x AB3000L 2.88 (measured 8.0-8.3)
MIN_SOC = 10                 # % fallback; the planner uses the device minSoc
MAX_SOC = 97                 # % fallback; the planner uses the device socSet (wear uses these)
CHARGE_MAX_W = 2000          # AC grid charging limit
DISCHARGE_MAX_W = 1600       # AC output limit
ETA_CHARGE = 0.93            # AC -> cells   (to be replaced by measured value)
ETA_DISCHARGE = 0.93         # cells -> AC   (to be replaced by measured value)
# wear per kWh delivered (AC) = price / lifetime energy. The EUR 2500 paid also
# covers the solar panels; the battery share is an assumption.
BATTERY_PRICE_EUR = 2000
BATTERY_CYCLES = 6000        # Zendure rating
AVG_HEALTH = 0.85            # average remaining capacity over its life
WEAR_EUR_PER_KWH = round(BATTERY_PRICE_EUR / (CAPACITY_KWH * (MAX_SOC - MIN_SOC) / 100
                                              * BATTERY_CYCLES * AVG_HEALTH * ETA_DISCHARGE), 3)

# --- house -------------------------------------------------------------------
# Expected house load per slot. Heat pump keeps load above the discharge limit
# in peak hours, so v1 assumes the battery can always deliver DISCHARGE_MAX_W.
HOUSE_LOAD_W = 1600

# --- planner -----------------------------------------------------------------
SLOT_MIN = 15
MIN_ACTION_W = 100           # planned power below this counts as BYPASS
END_VALUE_QUANTILE = 0.25    # value of energy left at horizon end: this price
                             # quantile x ETA_DISCHARGE - wear

# --- executor ----------------------------------------------------------------
# DRY_RUN = compute and log commands, never send them. Keep True while the
# Zendure app's own mode (Zenki) is still in control.
EXECUTOR_DRY_RUN = False
EXEC_INTERVAL_S = 2          # control loop period
WRITE_MIN_INTERVAL_S = 6     # settle time after a power change (device ramp + P1 delay)
TARGET_GRID_W = 10           # discharge aims for this much import (small margin against export)
DEADBAND_W = 15              # ignore smaller output corrections
# fuse guard (guard.py): the meter reports watts, not amps, so keep a margin
# below the fuse for power factor, low mains voltage and reaction time
BATTERY_PHASE = 2            # index into phases_w: L3
FUSE_A = 25
SAFE_A = 24
LINE_V = 230
SAFE_PHASE_W = SAFE_A * LINE_V   # 5520 W
GUARD_WINDOW_S = 30          # use the highest other load seen in this window
GUARD_MIN_DATA_S = 10        # data needed before charging may start
GUARD_RAISE_S = 30           # min time between raising the charge power
GUARD_DEADBAND_W = 50        # ignore smaller corrections
GUARD_AC_LAG_S = 6           # the P1 may still show battery power from this long ago
P1_STALE_S = 10              # older P1 data -> stop discharging
PLAN_STALE_S = 20 * 60       # older plan -> BYPASS
