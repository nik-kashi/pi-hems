"""Shared HEMS settings. All tunables live here."""

# --- devices (local network) ---------------------------------------------
SOLARFLOW = "http://192.168.2.32"
P1_METER = "http://192.168.2.34"

# --- location (approximate, for sunrise/sunset only) -------------------------
LAT, LON = 52.1, 5.1

# --- battery -----------------------------------------------------------------
CAPACITY_KWH = 6.72          # AB3000L 2.88 + 2x AB2000 1.92 (assumed; ~7.0 measured)
MIN_SOC = 10                 # % (matches device minSoc)
MAX_SOC = 97                 # % (matches device socSet)
CHARGE_MAX_W = 2000          # AC grid charging limit
DISCHARGE_MAX_W = 1600       # AC output limit
ETA_CHARGE = 0.93            # AC -> cells   (to be replaced by measured value)
ETA_DISCHARGE = 0.93         # cells -> AC   (to be replaced by measured value)
WEAR_EUR_PER_KWH = 0.03      # battery wear per kWh taken out of the battery (AC)

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
EXECUTOR_DRY_RUN = True
EXEC_INTERVAL_S = 2          # control loop period
WRITE_MIN_INTERVAL_S = 4     # min time between power-limit writes (device lag)
TARGET_GRID_W = 30           # discharge aims for this much import (never export)
DEADBAND_W = 25              # ignore smaller output corrections
PHASE_LIMIT_W = 5500         # reduce grid charging if any phase exceeds this
P1_STALE_S = 10              # older P1 data -> stop discharging
PLAN_STALE_S = 20 * 60       # older plan -> BYPASS
