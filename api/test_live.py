"""Checks for the today-so-far re-plan (live.py) and the measured-charge option of the optimiser. Run:  python test_live.py"""
import math
import numpy as np
import live, reopt
from optimiser import optimise
from test_reference_data import make

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))
D = make()
CLOUD = [{"what": "pv", "from_h": 10, "to_h": 15, "factor": 0.4}]
NONE = [{"what": "pv", "from_h": 0, "to_h": 24, "factor": 1.0}]

# 1. no deviation: nothing to correct, nothing to gain
r = live.replan({**D, "replay": {"now_step": 28, "scenario": NONE}})
ok("no deviation: forecast error is zero", r["forecast_error"]["load"]["mae_kw"] < 1e-9 and abs(r["forecast_error"]["pv"]["ratio"] - 1) < 1e-9)
ok("no deviation: old and new plan cost the same as the day-ahead plan", abs(r["expected"]["new_plan"]["cost"] - r["day_ahead_expected_cost"]) < 0.5 and abs(r["expected"]["old_plan"]["cost"] - r["day_ahead_expected_cost"]) < 0.5)

# 2. cloud seen so far: error measured, correction applied, re-plan is no worse on the same belief
r = live.replan({**D, "replay": {"now_step": 28, "scenario": CLOUD}})
ok("cloud: solar ratio measured below 1", r["forecast_error"]["pv"]["ratio"] < 0.8)
ok("cloud: load unaffected", abs(r["forecast_error"]["load"]["ratio"] - 1) < 1e-9)
ok("cloud: new plan expected cost <= old plan on the same corrected forecast", r["expected"]["new_plan"]["cost"] <= r["expected"]["old_plan"]["cost"] + 0.5)
ok("cloud: saving is calculated from the two costs", abs(r["expected"]["saving_rm"] - (r["expected"]["old_plan"]["cost"] - r["expected"]["new_plan"]["cost"])) < 1e-6)
ok("output is labelled a simulated replay", r["simulated"] and "SIMULATED" in r["data_kind"])
ok("both plans carry the same solar (same corrected forecast)", abs(r["expected"]["old_plan"]["solar_available_kwh"] - r["expected"]["new_plan"]["solar_available_kwh"]) < 1e-6)
ok("next steps start at the current step", r["next_steps"][0]["step"] == 28 and len(r["next_steps"]) == 8)
ok("elapsed steps in the series are the 'actual' ones", len(r["series"]["actual_pv"]) == 28 and len(r["series"]["belief_pv"]) == 48)

# 3. measured data: same result as the replay it was cut from, and the data checks work
n, load, pv, al, ap, every, trig, ups, tau, guard, desc = reopt._parse({**D, "scenario": CLOUD})
_, ex, _, _, _ = reopt._run(dict(D), n, load, pv, al, ap, every, trig, ups, tau, False, True)
k = 28
now = {"load": list(al[:k]), "pv": list(ap[:k]), "battery_kw": [ex["dis"][t] - ex["ch"][t] for t in range(k)], "ev_kw": ex["flex"][:k], "grid_kw": ex["grid"][:k]}
m = live.replan({**D, "now": now})
ok("measured mode gives the same expected costs as the replay of the same day", abs(m["expected"]["new_plan"]["cost"] - r["expected"]["new_plan"]["cost"]) < 1.0)
ok("measured: not labelled simulated", (not m["simulated"]) and m["data_quality"]["battery_source"] == "measured")
ok("measured grid agrees with the energy balance (gap ~0)", m["data_quality"]["grid_gap_kw"] < 0.01 and not m["data_quality"]["warn"])
bad = live.replan({**D, "now": {**now, "grid_kw": [g + 400 for g in now["grid_kw"]]}})
ok("measured grid that disagrees with the balance is flagged", bad["data_quality"]["warn"] and bad["data_quality"]["grid_gap_kw"] > 300)
ok("elapsed battery history is respected by the new plan", all(abs(m["series"]["new_ch"][t] - max(-now["battery_kw"][t], 0)) < 0.01 and abs(m["series"]["new_dis"][t] - max(now["battery_kw"][t], 0)) < 0.01 for t in range(k)))
ok("spent so far equals energy cost of the elapsed grid import", abs(m["spent_so_far_rm"] - sum(D["rates"][t] * ex["grid"][t] for t in range(k)) * 0.5) < 0.5)

# 4. measured battery charge changes the advice
lo = live.replan({**D, "now": {**now, "soc_kwh": 300.0}})
hi = live.replan({**D, "now": {**now, "soc_kwh": 4500.0}})
ok("an almost empty battery discharges less in the next hours than a full one", sum(x["battery_kw"] for x in lo["next_steps"][:4]) < sum(x["battery_kw"] for x in hi["next_steps"][:4]))
ok("soc source is reported as measured", lo["data_quality"]["soc_source"] == "measured")
# optimiser level: SOC at step k really equals the measurement
req = {**D, "load": list(np.concatenate([al[:k], load[k:]])), "pv": list(np.concatenate([ap[:k], pv[k:]])),
       "fixed": {"ch": [max(-b, 0) for b in now["battery_kw"]], "dis": [max(b, 0) for b in now["battery_kw"]], "flex": ex["flex"][:k]}, "soc_now_kwh": 1200.0}
o = optimise(req)
soc = 1200.0; eta = math.sqrt(.9); mn = soc
for t in range(k, 48):
    soc += eta * .5 * o["ch"][t] - .5 / eta * o["dis"][t]; mn = min(mn, soc)
ok("future charge never goes below zero when started from the measured charge", mn > -1.0 and soc >= 2500 - 2.0)

# 5. bad input
for bad_req, why in (({"now": {"load": [1.0] * 28}}, "pv missing"), ({"now": {"load": [1.0] * 28, "pv": [1.0] * 10}}, "pv wrong length"),
                     ({"now": {"load": []}}, "empty load"), ({"now": {"load": [1.0] * 48, "pv": [1.0] * 48}}, "a full day (nothing left to plan)"),
                     ({"now": {"load": [-5.0] * 4, "pv": [1.0] * 4}}, "negative load"), ({"now": {"load": ["x"] * 4, "pv": [1.0] * 4}}, "text in load"),
                     ({"now": {**now, "soc_kwh": 99999}}, "charge above battery size"), ({"replay": {"now_step": 0, "scenario": NONE}}, "replay at step 0"),
                     ({}, "no data at all"), ({"now": {**now}, "persistence_hours": 500}, "persistence 500 h")):
    try:
        live.replan({**D, **bad_req}); ok("rejected: " + why, False)
    except ValueError:
        ok("rejected: " + why, True)
try:
    optimise({**D, "soc_now_kwh": 100}); ok("rejected: soc_now_kwh without fixed steps", False)
except ValueError:
    ok("rejected: soc_now_kwh without fixed steps", True)

print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
