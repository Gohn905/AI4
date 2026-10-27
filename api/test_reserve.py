"""Checks for the battery reserve (soc_reserve). Run:  python test_reserve.py   (needs numpy and scipy)"""
import math
import numpy as np
from optimiser import optimise
from forecast import SHAPES

H = [t / 2 for t in range(48)]
SH = SHAPES["campus"]
LOAD = [4000 * SH[int(h)] for h in H]
RATES = [0.3132 if 14 <= h < 22 else 0.2723 for h in H]
EV = {"chargers": 8, "kw": 50, "energy_kwh": 700, "window": [7.5, 18.0]}
def pv(kwp): return [max(0.0, kwp * 0.78 * math.sin(math.pi * (h - 6.5) / 12)) if 6.5 < h < 18.5 else 0.0 for h in H]
def req(kwp, rsv, ca, wr=0.0):
    return {"load": LOAD, "pv": pv(kwp), "rates": RATES, "md_rate_day": 97.06 / 30, "md_hours": list(range(14, 22)), "batt_kwh": 5000,
            "flex_share": 0, "carbon_adder": 0, "grid_limit": 4200, "hard_limit": True, "wear": wr, "eff": 0.90, "co2_allow": ca,
            "soc_reserve": rsv, "ev": EV}
def soc(r, cap=5000, eta=math.sqrt(0.9)):
    s = [0.5 * cap]
    for c, d in zip(r["ch"], r["dis"]): s.append(s[-1] + eta * c * 0.5 - d * 0.5 / eta)
    return np.array(s)

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))

# 1. the reserve is respected all day
r = optimise(req(3000, 0.3, 0))
ok("30% reserve: battery charge never below 1500 kWh", soc(r)[1:-1].min() >= 1500 - 1.0)
ok("limit still met with reserve (3 MWp)", r["limit_ok"] is True)

# 2. a reserve costs money (it holds back peak shaving), and never saves money
a, b = optimise(req(3000, 0, 1)), optimise(req(3000, 0.2, 1))
ok("reserve never makes the day cheaper", b["cost"] >= a["cost"] - 0.01)

# 3. with a lot of surplus solar the three strategies now differ (they were identical before)
costs = {k: round(optimise(req(5000, rs, ca, wr))["cost"], 2) for k, (rs, ca, wr) in {"cost": (0, 1, 0), "bal": (0.2, 0.5, 0.02), "zero": (0.3, 0, 0.02)}.items()}
print("      surplus-solar costs:", costs)
ok("surplus solar: Cost-saving < Balanced < Lowest-carbon (three different plans)", costs["cost"] < costs["bal"] < costs["zero"])

# 4. reserve of 0 behaves exactly as before
c0 = optimise({k: v for k, v in req(3000, 0, 1).items() if k != "soc_reserve"})
ok("soc_reserve 0 gives the same cost as leaving it out", abs(c0["cost"] - a["cost"]) < 0.01)

# 5. the grid limit wins over the reserve
tight = req(3000, 0.5, 1); tight["grid_limit"] = 1600     # a limit the battery can only meet by dipping into the reserve
t = optimise(tight)
ok("a hard grid limit is still met, by giving up the reserve, and the result says so", t["limit_ok"] is True and t["reserve_relaxed"] is True)
easy = optimise(req(3000, 0.3, 1))
ok("with an easy limit the reserve is kept (reserve_relaxed false)", easy["reserve_relaxed"] is False)

# 6. bad input is refused
try:
    optimise(req(3000, 0.9, 1)); bad = False
except ValueError:
    bad = True
ok("reserve above 50% is rejected", bad)

print(f"\n{sum(res)}/{len(res)} passed")
raise SystemExit(0 if all(res) else 1)
