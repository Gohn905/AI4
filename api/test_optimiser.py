"""Checks for the optimiser's CO2 controls. Run:  python test_optimiser.py   (needs numpy and scipy)"""
import math
import numpy as np
from optimiser import optimise

CAMP = [350, 345, 345, 345, 345, 345, 380, 450, 700, 850, 880, 900, 780, 970, 1050, 1010, 880, 750, 600, 500, 420, 380, 360, 355]
LOAD = np.array(CAMP, float)
PV = np.array([max(0, 300 * 0.8 * math.sin(math.pi * (h - 6) / 12)) if 6 < h < 18 else 0 for h in range(24)])
RATES = [0.3132 if 14 <= h < 22 else 0.2723 for h in range(24)]
MDD = 97.06 / 30
BASE_G = float(np.maximum(LOAD - PV, 0).sum())
BASE_COST = float(np.dot(RATES, np.maximum(LOAD - PV, 0))) + MDD * max(np.maximum(LOAD - PV, 0)[14:22])


def run(**kw):
    d = {"load": LOAD.tolist(), "pv": PV.tolist(), "rates": RATES, "md_rate_day": MDD, "md_hours": list(range(14, 22)),
         "batt_kwh": 500, "flex_share": 0.10, "carbon_adder": 0, "grid_limit": 0, "wear": 0}
    d.update(kw)
    r = optimise(d)
    r["kwh"] = float(sum(r["grid"]))
    r["saving"] = BASE_COST - (float(np.dot(RATES, r["grid"])) + MDD * r["peak"])
    return r


res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))

free = run()
ok("cheapest plan saves money and adds grid energy (battery losses)", free["saving"] > 700 and free["kwh"] > BASE_G + 20)
half, zero = run(co2_allow=0.5), run(co2_allow=0)
ok("co2_allow 0: no extra grid energy (within 0.5 kWh)", zero["kwh"] <= BASE_G + 0.5)
ok("co2_allow 0.5: about half of the extra grid energy", abs((half["kwh"] - BASE_G) - 0.5 * (free["kwh"] - BASE_G)) < 0.05 * (free["kwh"] - BASE_G) + 1)
ok("saving falls as less CO2 is allowed: 1 >= 0.5 >= 0 > 0", free["saving"] >= half["saving"] - 1 >= zero["saving"] - 2 and zero["saving"] > 0)
ok("no-extra-CO2 plan still saves real money (about RM 343 in this case)", 300 < zero["saving"] < 390)
ok("co2_allow 1 behaves exactly like no setting", abs(run(co2_allow=1)["saving"] - free["saving"]) < 1e-6)
tiny = run(grid_kwh_cap=1.0)
ok("an impossible cap never breaks the plan (soft cap reports the excess)", tiny["cap_over_kwh"] > 100)
e90, e80, e100 = run(eff=0.9), run(eff=0.8), run(eff=1.0)
ok("lower battery efficiency means more grid energy", e80["kwh"] > e90["kwh"] > e100["kwh"])
ok("a perfect battery adds almost no extra grid energy", e100["kwh"] - BASE_G < 5)
ok("default efficiency is 90%", abs(run()["kwh"] - e90["kwh"]) < 1e-6)
for bad, why in (({"eff": 0.3}, "efficiency too low"), ({"eff": 1.5}, "efficiency above 1"), ({"co2_allow": 2}, "co2_allow above 1"), ({"co2_allow": -0.1}, "co2_allow below 0"), ({"grid_kwh_cap": -5}, "negative cap")):
    try: run(**bad); ok("rejected: " + why, False)
    except ValueError: ok("rejected: " + why, True)
print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
