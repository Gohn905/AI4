"""Checks for the re-optimisation simulation (reopt.py). Run:  python test_reopt.py   (needs numpy and scipy)"""
import math
import numpy as np
import reopt
from test_reference_data import make

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))
D = make()
CLOUD = [{"what": "pv", "from_h": 12, "to_h": 15, "factor": 0.4}]
LOW = [{"what": "pv", "from_h": 0, "to_h": 24, "factor": 0.5}]

# 1. no deviation: re-planning changes nothing
r = reopt.simulate({**D, "scenario": [{"what": "pv", "from_h": 0, "to_h": 24, "factor": 1.0}]})
R = r["results"]
ok("no deviation: open-loop = rolling = perfect foresight (cost)", abs(R["open_loop"]["cost"] - R["rolling"]["cost"]) < 0.5 and abs(R["rolling"]["cost"] - R["perfect_foresight"]["cost"]) < 0.5)
ok("no deviation: realised cost equals the day-ahead expected cost", abs(R["open_loop"]["cost"] - r["day_ahead_expected_cost"]) < 0.5)

# 2. every strategy is judged on the same actual day with the same KPI function
r = reopt.simulate({**D, "scenario": CLOUD})
R = r["results"]
ok("same actual solar for every strategy", len({round(R[k]["solar_available_kwh"], 3) for k in R}) == 1)
ok("actual solar is lower than forecast in a cloud scenario", R["solar_only"]["solar_available_kwh"] < sum(D["pv"]) * 0.5 - 1)
ok("energy balances in every step for every executed strategy", all(R[k]["balance_error_kw"] < 0.01 for k in R))
ok("replanning log exists and starts after step 0", len(r["replans"]) > 0 and all(l["step"] > 0 for l in r["replans"]))
ok("cloud: realised cost is above the day-ahead expectation (forecast error costs money)", r["summary"]["forecast_error_cost_rm"] > 100)
ok("perfect foresight is the lowest cost (it is a bound)", R["perfect_foresight"]["cost"] <= min(R["open_loop"]["cost"], R["rolling"]["cost"]) + 0.5)

# 3. where solar falls short, re-planning helps and holds the limit better
for name, sc in (("cloud front", CLOUD), ("half solar all day", LOW)):
    q = reopt.simulate({**D, "scenario": sc})["results"]
    ok(f"{name}: rolling costs less than following the old plan", q["rolling"]["cost"] < q["open_loop"]["cost"] - 50)
    ok(f"{name}: rolling has no more limit violations than open-loop", q["rolling"]["limit_violation_steps"] <= q["open_loop"]["limit_violation_steps"])
    ok(f"{name}: rolling still beats the best simple reference", q["rolling"]["cost"] < min(q[k]["cost"] for k in ("solar_only", "battery_self", "battery_tou")))

# 4. no look-ahead: changing the actual day AFTER step 30 must not change anything decided or executed before step 30
n, load, pv, al, ap, every, trig, ups, tau, guard, desc = reopt._parse({**D, "scenario": CLOUD})
ap2, al2 = ap.copy(), al.copy()
ap2[30:] *= 0.1; al2[30:] *= 1.5
base = {k: v for k, v in D.items()}
_, ex1, log1, _, _ = reopt._run(base, n, load, pv, al, ap, every, trig, ups, tau, True)
_, ex2, log2, _, _ = reopt._run(base, n, load, pv, al2, ap2, every, trig, ups, tau, True)
ok("no look-ahead: executed grid before step 30 identical when only the future changes", np.allclose(ex1["grid"][:30], ex2["grid"][:30], atol=1e-6))
ok("no look-ahead: executed battery before step 30 identical", np.allclose(ex1["dis"][:30], ex2["dis"][:30], atol=1e-6) and np.allclose(ex1["ch"][:30], ex2["ch"][:30], atol=1e-6))
ok("no look-ahead: re-plan log before step 30 identical", [l for l in log1 if l["step"] < 30] == [l for l in log2 if l["step"] < 30])

# 5. physical rules hold in the executed day
_, ex, _, _, soc_end = reopt._run(base, n, load, pv, al, ap, every, trig, ups, tau, True)
soc, okk = 2500.0, True
for c, d_ in zip(ex["ch"], ex["dis"]):
    soc += math.sqrt(.9) * .5 * c - .5 / math.sqrt(.9) * d_
    okk &= -1e-6 <= soc <= 5000 + 1e-6
ok("executed day keeps battery state of charge between 0 and 5000 kWh", okk)
ok("executed battery power within 2500 kW", max(ex["ch"]) <= 2500 + 1e-6 and max(ex["dis"]) <= 2500 + 1e-6)
ok("executed EV charging only inside the 07:30-18:00 window", sum(ex["flex"][:15]) == 0 and sum(ex["flex"][36:]) == 0)
ok("executed EV energy is the 700 kWh asked for", abs(sum(ex["flex"]) * .5 - 700) < 1.0)
ok("solar used never exceeds actual solar", all(u <= p + 1e-6 for u, p in zip(ex["pv_used"], ap)))
ok("no export: grid import is never negative", min(ex["grid"]) >= -1e-9)

# 5b. reactive limit guard: applied to both executed strategies, and it never breaks battery physics
g1 = reopt.simulate({**D, "scenario": LOW})["results"]
g0 = reopt.simulate({**D, "scenario": LOW, "replan": {"guard": False}})["results"]
ok("guard on: open-loop has no more limit violations than with the guard off", g1["open_loop"]["limit_violation_steps"] <= g0["open_loop"]["limit_violation_steps"])
ok("guard on: the unguarded open-loop day does exceed the limit (so the test means something)", g0["open_loop"]["limit_violation_steps"] > 0)
ok("guard on: open-loop excess over the limit is smaller", g1["open_loop"]["limit_max_excess_kw"] <= g0["open_loop"]["limit_max_excess_kw"] + 1e-6)
ok("guard on: energy still balances", g1["open_loop"]["balance_error_kw"] < 0.01 and g1["rolling"]["balance_error_kw"] < 0.01)
st = reopt._step(0.0, 0.0, 0.0, 6000.0, 0.0, 2000.0, 5000.0, math.sqrt(.9), .5, 4200.0)
ok("guard: a 6000 kW load with 4200 kW limit is cut to the limit by discharging", abs(st[0] - 4200.0) < 1e-6 and st[3] > 1799)
st = reopt._step(1500.0, 0.0, 0.0, 3000.0, 0.0, 2000.0, 5000.0, math.sqrt(.9), .5, 3500.0)
ok("guard: planned grid charging is cancelled before extra discharge", st[2] < 1500.0 and st[0] <= 3500.0 + 1e-6)
st = reopt._step(0.0, 0.0, 0.0, 6000.0, 0.0, 0.0, 5000.0, math.sqrt(.9), .5, 4200.0)
ok("guard: an empty battery cannot help, import stays above the limit and is reported", st[0] > 4200.0 and st[3] == 0.0)

# 6. new forecast arrives: it is used from that step on, and only from then
good = {"at_step": 24, "pv": list(ap), "load": list(al)}            # at 12:00 the forecast is corrected to the real day
u = reopt.simulate({**D, "scenario": CLOUD, "replan": {"every_steps": 0, "trigger_kw": 0, "updates": [good]}})
ok("a forecast update triggers exactly one re-plan, labelled", [(l["step"], l["reason"]) for l in u["replans"]] == [(24, "new forecast")])
ok("a correct forecast update does not make the day worse", u["results"]["rolling"]["cost"] <= u["results"]["open_loop"]["cost"] + 0.5)

# 7. triggers
q = reopt.simulate({**D, "scenario": CLOUD, "replan": {"every_steps": 0, "trigger_kw": 200}})
ok("trigger only: re-plans happen only after a forecast error", len(q["replans"]) > 0 and all(l["reason"] == "forecast error" for l in q["replans"]))
q = reopt.simulate({**D, "scenario": [{"what": "pv", "from_h": 0, "to_h": 24, "factor": 1.0}], "replan": {"every_steps": 0, "trigger_kw": 200}})
ok("trigger only: no deviation means no re-plan", q["replans"] == [])
ok("timer: every_steps=8 re-plans every 4 hours", [l["step"] for l in reopt.simulate({**D, "scenario": CLOUD, "replan": {"every_steps": 8, "trigger_kw": 0}})["replans"]] == [8, 16, 24, 32, 40])

# 8. explicit actuals
q = reopt.simulate({**D, "actual": {"load": D["load"], "pv": [v * .6 for v in D["pv"]]}})
ok("explicit actual series are accepted and labelled", "actual series supplied" in q["data_kind"] and q["results"]["solar_only"]["solar_available_kwh"] < sum(D["pv"]) * .5 * .7)
ok("output is labelled a simulation", "SIMULATION" in q["data_kind"])

# 9. bad input
for bad, why in (({}, "no scenario and no actual"), ({"scenario": [{"what": "wind", "from_h": 1, "to_h": 2, "factor": 1}]}, "unknown scenario type"),
                 ({"scenario": [{"what": "pv", "from_h": 5, "to_h": 3, "factor": 1}]}, "scenario hours reversed"),
                 ({"scenario": [{"what": "pv", "from_h": 1, "to_h": 3, "factor": 50}]}, "scenario factor 50"),
                 ({"scenario": CLOUD, "replan": {"every_steps": 99}}, "every_steps above steps"),
                 ({"scenario": CLOUD, "replan": {"updates": [{"at_step": 0}]}}, "update at step 0"),
                 ({"actual": {"load": [1] * 48, "pv": [1] * 24}}, "actual of the wrong length")):
    try:
        reopt.simulate({**D, **bad}); ok("rejected: " + why, False)
    except ValueError:
        ok("rejected: " + why, True)

print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
