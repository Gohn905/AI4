"""Checks for the fair comparison (reference.py) and the optimiser's 'fixed' steps. Run:  python test_reference.py   (needs numpy and scipy)"""
import math
import numpy as np
from optimiser import optimise
import reference as ref

from test_reference_data import make

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))

D = make()
C = ref.compare(D)
S, BS, BT, O = C["solar_only"], C["battery_self"], C["battery_tou"], C["optimised"]

# energy balance: grid + discharge + solar used = load + charging, in every step, for every strategy
for k in ("solar_only", "battery_self", "battery_tou"):
    ok(f"{k}: energy balances in every step", C[k]["balance_error_kw"] < 1e-6)
ok("optimised: energy balances in every step (rounding < 0.01 kW)", O["balance_error_kw"] < 0.01)

# same inputs: every strategy serves the same site load + the same EV energy
ev_kwh = lambda x: (sum(x["load"]) - sum(D["load"])) * 0.5
ok("all four serve the same EV energy (700 kWh)", all(abs(ev_kwh(C[k]) - 700) < 0.5 for k in C if k in ("solar_only", "battery_self", "battery_tou", "optimised")))
ok("solar available is identical for all strategies", len({round(C[k]["solar_available_kwh"], 3) for k in ("solar_only", "battery_self", "battery_tou", "optimised")}) == 1)

# battery rules obey physics
for k in ("battery_self", "battery_tou"):
    x = C[k]
    ok(f"{k}: battery power never above 2500 kW", max(x["ch"]) <= 2500 + 1e-6 and max(x["dis"]) <= 2500 + 1e-6)
    soc, okk = 2500.0, True
    for c, dd in zip(x["ch"], x["dis"]):
        soc += math.sqrt(.9) * 0.5 * c - 0.5 / math.sqrt(.9) * dd
        okk &= -1e-6 <= soc <= 5000 + 1e-6
    ok(f"{k}: state of charge stays between 0 and 5000 kWh", okk)
ok("self-consumption battery never charges from the grid", all(c <= max(p - l, 0) + 1e-6 for c, p, l in zip(BS["ch"], D["pv"], BS["load"])))
ok("a battery rule that ends below 50% is charged for the shortfall", BS["soc_shortfall_kwh"] == 0 or BS["soc_adjust_cost"] > 0)

# fairness: the optimiser is never worse than the best reference on the real bill (no limit, no wear, so the references are feasible plans)
for name, d in (("weekday", make(lim=0)), ("sunny low-load weekday", make(peak=2500, kwp=4000, lim=0)), ("cloudy weekday", make(cloud=.4, lim=0)),
                ("Sunday", make(wk=False, ev=False, lim=0)), ("no battery", make(bat=0, lim=0))):
    c = ref.compare(d)
    best = min(c[k]["cost"] for k in ("solar_only", "battery_self", "battery_tou"))
    ok(f"optimised cost <= best reference ({name})", c["optimised"]["cost"] <= best + 0.5)
c0 = ref.compare(make(bat=0, lim=0))
ok("with no battery and no flexibility to use, optimised = solar only except EV timing", c0["optimised"]["cost"] <= c0["solar_only"]["cost"] + 0.5)

# savings are calculated, not typed in
sv = C["savings"]
ok("percentage saving matches the costs", abs(sv["optimised_vs_solar_only_pct"] - 100 * (S["cost"] - O["cost"]) / S["cost"]) < 1e-9)
ok("best reference is the cheapest of the three", sv["best_reference_cost"] == min(S["cost"], BS["cost"], BT["cost"]))

# KPIs: peak, limit, curtailment
ok("billed peak is the highest import inside the demand-charge hours", abs(S["billed_peak_kw"] - max(S["grid"][28:44])) < 1e-9)
over = make(peak=7000, lim=4200)
cs = ref.solar_only(over)
ok("limit violations are counted for a strategy that ignores the limit", cs["limit_violation_steps"] > 0 and cs["limit_max_excess_kw"] > 0)
sunny = ref.solar_only(make(peak=1500, kwp=4000, ev=False))
ok("surplus solar with no battery is reported as curtailed", sunny["solar_curtailed_kwh"] > 100 and abs(sunny["solar_used_kwh"] + sunny["solar_curtailed_kwh"] - sunny["solar_available_kwh"]) < 1e-6)
ok("self-consumption is used / available", abs(sunny["solar_self_consumption_pct"] - 100 * sunny["solar_used_kwh"] / sunny["solar_available_kwh"]) < 1e-9)

# the optimiser can hold the 4.2 MW limit where solar-only cannot
hi = make(peak=7000, lim=4200)
ch = ref.compare(hi)
ok("heavy-load day: solar only breaks the limit, optimiser holds it", ch["solar_only"]["limit_violation_steps"] > 0 and ch["optimised"]["limit_violation_steps"] == 0)

# invalid input
for bad, why in (({"eff": 0.2}, "efficiency 0.2"), ({"load": [1] * 30}, "30-step load"), ({"rates": [11] * 48}, "rate above 10")):
    try:
        ref.compare({**D, **bad}); ok("rejected: " + why, False)
    except ValueError:
        ok("rejected: " + why, True)

# optimiser 'fixed' steps: the first k steps are locked to what happened
base = optimise(D)
k = 20
fx = {key: base[key][:k] for key in ("ch", "dis", "flex")}
r = optimise({**D, "fixed": fx})
ok("fixed steps are reproduced exactly", all(abs(r[key][t] - fx[key][t]) < 1e-6 for key in fx for t in range(k)))
ok("re-solving with the optimum's own past gives the same cost", abs(r["cost"] - base["cost"]) < 0.5)
bad_fx = {**fx, "dis": [0.0] * k, "ch": [2000.0] * k}
try:
    r2 = optimise({**D, "fixed": bad_fx}); ok("a worse past cannot lower the cost below the unconstrained optimum", r2["cost"] >= base["cost"] - 0.5)
except RuntimeError:
    ok("an impossible past raises a clear error", True)
for bf, why in (({**fx, "ch": fx["ch"][:5]}, "fixed lists of different length"), ({**fx, "ch": [float("nan")] * k}, "NaN in fixed"), ({"grid": []}, "empty fixed")):
    try:
        optimise({**D, "fixed": bf}); ok("rejected: " + why, False)
    except ValueError:
        ok("rejected: " + why, True)

print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
