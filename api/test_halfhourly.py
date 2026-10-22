"""Checks for half-hourly (48-step) optimisation. Run:  python test_halfhourly.py   (needs numpy and scipy)"""
import math
import numpy as np
from optimiser import optimise

CAMP = [350, 345, 345, 345, 345, 345, 380, 450, 700, 850, 880, 900, 780, 970, 1050, 1010, 880, 750, 600, 500, 420, 380, 360, 355]
PV = [max(0, 300 * 0.8 * math.sin(math.pi * (h - 6) / 12)) if 6 < h < 18 else 0 for h in range(24)]
RATES = [0.3132 if 14 <= h < 22 else 0.2723 for h in range(24)]
BASE = {"load": CAMP, "pv": PV, "rates": RATES, "md_rate_day": 97.06 / 30, "md_hours": list(range(14, 22)),
        "batt_kwh": 500, "flex_share": 0.10, "carbon_adder": 0, "grid_limit": 0, "wear": 0.02}
twice = lambda v: np.repeat(np.asarray(v, float), 2).tolist()
H48 = {**BASE, "load": twice(CAMP), "pv": twice(PV), "rates": twice(RATES)}

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))

h, hh = optimise(BASE), optimise(H48)
ok("hourly input still returns 24 steps", h["steps"] == 24 and len(h["grid"]) == 24)
ok("half-hourly input returns 48 steps", hh["steps"] == 48 and len(hh["grid"]) == 48 and len(hh["ld"]) == 48)
ok("hourly data repeated as 48 steps gives the same cost and billed peak",
   abs(h["cost"] - hh["cost"]) < 0.01 and abs(h["peak"] - hh["peak"]) < 0.01)
ok("hourly result unchanged from before (cost RM 5532.77, peak 601.9 kW)", abs(h["cost"] - 5532.77) < 0.01 and abs(h["peak"] - 601.935) < 0.01)

# a 30-minute spike inside the billed window: hourly averaging hides half of it
L48 = np.asarray(twice(CAMP))
L48[30] += 400                                            # 15:00-15:30 only
L24 = [(L48[2 * i] + L48[2 * i + 1]) / 2 for i in range(24)]
plain = {"batt_kwh": 0, "flex_share": 0}
s48 = optimise({**H48, **plain, "load": L48.tolist()})
s24 = optimise({**BASE, **plain, "load": L24})
ok("half-hourly sees a short spike that hourly averaging hides (higher billed peak)", s48["peak"] > s24["peak"] + 150)
ok("the extra billed peak is the spike itself (about 200 kW above the hourly average)", abs((s48["peak"] - s24["peak"]) - 200) < 5)

# the battery can shave the spike in the half-hourly model
b48 = optimise({**H48, "load": L48.tolist(), "flex_share": 0})
ok("battery lowers the half-hourly billed peak", b48["peak"] < s48["peak"] - 100)
ok("battery limits are respected per step (<= half the capacity in kW)", max(b48["ch"]) <= 250.001 and max(b48["dis"]) <= 250.001)

# energy conservation in kWh (kW x 0.5 h per step)
dt = 0.5
supply = np.array(b48["grid"]) + np.array(b48["dis"]) + np.array(b48["pv_used"])
use = np.array(b48["ld"]) + np.array(b48["ch"])
ok("power balance holds in every half-hour", np.allclose(supply, use, atol=0.01))
ok("shiftable energy is unchanged (kWh)", abs(sum(b48["ld"]) * dt - sum(L48) * dt * 1) < 1e-6 + 0.01 * sum(L48))

for bad, why in (({"load": CAMP[:23]}, "23 values"), ({"load": CAMP + [1]}, "25 values"),
                 ({"pv": twice(PV)}, "pv length different from load")):
    try:
        optimise({**BASE, **bad}); ok("rejected: " + why, False)
    except ValueError:
        ok("rejected: " + why, True)
print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
