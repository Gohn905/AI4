"""Checks for the EV charger rules and the hard grid limit. Run:  python test_ev_limit.py   (needs numpy and scipy)"""
import math
import numpy as np
from optimiser import optimise, ev_baseline, operating_window

H = [t / 2 for t in range(48)]                                    # hour of each half-hour step
LOAD = [3000 + 2800 * math.exp(-((h - 15) ** 2) / 8) for h in H]  # about 5.8 MW at 15:00, so net grid import passes 4.2 MW without a battery
PV = [max(0.0, 1500 * math.sin(math.pi * (h - 6.5) / 12)) if 6.5 < h < 18.5 else 0.0 for h in H]
RATES = [0.3132 if 14 <= h < 22 else 0.2723 for h in H]
BASE = {"load": LOAD, "pv": PV, "rates": RATES, "md_rate_day": 97.06 / 30, "md_hours": list(range(14, 22)),
        "batt_kwh": 5000, "flex_share": 0, "carbon_adder": 0, "wear": 0.02, "eff": 0.90}
EV = {"chargers": 8, "kw": 50, "energy_kwh": 1200, "window": [7.5, 18.0]}

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))

# operating hours
ok("Mon-Fri window is 07:30-18:00", all(operating_window(d) == (7.5, 18.0) for d in range(5)))
ok("Saturday window is 07:30-14:00, Sunday is closed", operating_window(5) == (7.5, 14.0) and operating_window(6) is None)

# EV charging respects window, power and energy
r = optimise({**BASE, "ev": EV})
flex = np.array(r["flex"])
ok("EV energy delivered equals the energy asked for (1200 kWh)", abs(flex.sum() * 0.5 - 1200) < 0.5 and r["ev_unmet_kwh"] == 0)
ok("no EV charging before 07:30 or after 18:00", flex[:15].sum() == 0 and flex[36:].sum() == 0)
ok("EV power never exceeds 8 x 50 = 400 kW", flex.max() <= 400.001)
ok("flexible load is only the chargers (base load unchanged)", np.allclose(r["ld"], np.array(LOAD) + flex, atol=0.01))

# Sunday / closed: nothing charges, shortfall is reported
rs = optimise({**BASE, "ev": {**EV, "window": None}})
ok("closed day: no EV charging and the whole demand is reported unmet", sum(rs["flex"]) == 0 and abs(rs["ev_unmet_kwh"] - 1200) < 1e-6)
# not enough hours/power for the energy
rt = optimise({**BASE, "ev": {**EV, "energy_kwh": 5000, "window": [7.5, 9.5]}})
ok("energy that cannot fit is capped at the window and reported", abs(sum(rt["flex"]) * 0.5 - 800) < 0.5 and abs(rt["ev_unmet_kwh"] - 4200) < 0.5)

# smart charging is never worse than charging immediately
nb = {**BASE, "batt_kwh": 0}
smart = optimise({**nb, "ev": EV})
naive_ev = np.array(ev_baseline({**nb, "ev": EV}))
naive_grid = np.maximum(np.array(LOAD) + naive_ev - np.array(PV), 0)
naive_cost = float(np.dot(RATES, naive_grid) * 0.5 + (97.06 / 30) * naive_grid[28:44].max())
ok("baseline charging delivers the same energy inside the window", abs(naive_ev.sum() * 0.5 - 1200) < 0.5 and naive_ev[:15].sum() == 0)
ok("smart EV charging costs no more than charging immediately", smart["cost"] <= naive_cost + 0.01)

# hard 4.2 MW limit
no_lim = optimise({**BASE, "batt_kwh": 0, "ev": EV})
ok("without a limit the demo day would exceed 4.2 MW (so the test is meaningful)", no_lim["max_grid"] > 4200)
lim = optimise({**BASE, "grid_limit": 4200, "ev": EV})
ok("with the limit, grid import never exceeds 4.2 MW and limit_ok is True", lim["max_grid"] <= 4200.0001 and lim["limit_ok"] is True)
ok("with the limit, power balance still holds", np.allclose(np.array(lim["grid"]) + lim["dis"] + lim["pv_used"], np.array(lim["ld"]) + lim["ch"], atol=0.01))
bad = optimise({**BASE, "batt_kwh": 100, "grid_limit": 1000, "ev": EV})
ok("an impossible limit does not crash: limit_ok is False and the excess is reported", bad["limit_ok"] is False and bad["max_grid"] > 1000)
ok("no limit given: limit_ok is True", optimise({**BASE, "ev": EV})["limit_ok"] is True)

# CO2-capped strategies must count the EV energy in their solar-only baseline (regression: Balanced used to save ~0%)
for allow in (0.5, 0):
    rc = optimise({**BASE, "ev": EV, "co2_allow": allow})
    ok(f"co2_allow {allow} with EVs: the grid-energy cap is not broken", rc["cap_over_kwh"] < 1e-6)

# EV charging is only moved when that lowers the bill (regression: pointless cost-neutral shifts used to appear in every strategy)
off = {"shift_penalty": 0}
for name, extra in (("Cost-saving", {}), ("Balanced", {"co2_allow": 0.5}), ("Lowest-carbon", {"co2_allow": 0})):
    on_ = optimise({**BASE, "ev": EV, **extra}); free = optimise({**BASE, "ev": EV, **extra, **off})
    moved = np.abs(np.array(on_["flex"]) - np.array(ev_baseline({**BASE, "ev": EV}))).sum() * 0.5 / 2
    ok(f"{name}: same bill with or without the shift tie-break", abs(on_["cost"] - free["cost"]) < 0.5)
    if name != "Lowest-carbon":
        ok(f"{name}: on this day no EV energy is moved when moving saves nothing", moved < 1)
# a shift that really pays must survive: big solar surplus at midday, small load, no battery
SUN = {**BASE, "load": [v * 0.25 for v in LOAD], "pv": PV, "batt_kwh": 0, "ev": {**EV, "energy_kwh": 1200}}
paid, nofix = optimise(SUN), optimise({**SUN, **off})
ok("a profitable shift is kept: same bill as without the tie-break", abs(paid["cost"] - nofix["cost"]) < 0.5)
eb = np.array(ev_baseline(SUN)); pf = np.array(paid["flex"])
ok("... and the chargers really move into the sunny hours (10:30-15:00), where baseline has none", pf[21:30].sum() * 0.5 > 100 and eb[21:30].sum() == 0)
try:
    optimise({**BASE, "ev": EV, "shift_penalty": 5}); ok("rejected: shift_penalty too large", False)
except ValueError:
    ok("rejected: shift_penalty too large", True)

for badev, why in (({"window": [18, 7]}, "window start after end"), ({"energy_kwh": -5}, "negative energy"), ({"chargers": "x"}, "text chargers"), ({"window": [7.5]}, "one-value window")):
    try:
        optimise({**BASE, "ev": {**EV, **badev}}); ok("rejected: " + why, False)
    except ValueError:
        ok("rejected: " + why, True)
print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
