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


# 10. the real-time chart rebuilds the whole day (recorded before now, predicted after) from the series: it must balance exactly
for scen in (NONE, CLOUD):
    for D2 in (D, {**D, "flex_share": 0.1}):
        r = live.replan({**D2, "replay": {"now_step": 28, "scenario": scen}}); s_ = r["series"]; n = r["steps"]; k = r["now_step"]
        Lh = np.array(s_["actual_load"] + s_["belief_load"][k:]); fl = np.array(s_["new_flex"])
        ld, pu = np.array(s_["new_ld"]), np.array(s_["new_pv_used"])
        ok("series: demand = recorded load before now + corrected load after + flexible load", len(ld) == n and np.allclose(ld, Lh + fl, atol=1e-3))
        ok("series: solar used = demand + charge - discharge - grid, and never more than the solar that exists",
           np.allclose(pu, ld + np.array(s_["new_ch"]) - np.array(s_["new_dis"]) - np.array(s_["new_grid"]), atol=1e-2)
           and np.all(pu <= np.array(s_["actual_pv"] + s_["belief_pv"][k:]) + 1e-3))


# 11. a re-plan must keep the SAME CO2 allowance as the day-ahead plan (regression: it used to be applied again to what was left of the
#     day, so with nothing wrong in the forecast the re-plan cost RM 754 MORE than simply carrying on with the old plan)
PAGE_DAY = {"batt_kwh":5000,"eff":0.9,"grid_limit":4200,"hard_limit":True,"md_rate_day":3.2353333333333336,"md_hours":[14,15,16,17,18,19,20,21],"flex_share":0,"carbon_adder":0,"wear":0.02,"ev":{"chargers":8,"kw":50,"energy_kwh":700,"window":[7.5,18]},"load":[1265.5,1260.3,1249.8,1246.8,1251.3,1251.2,1246.5,1244.5,1245.1,1244.3,1241.9,1270.4,1329.7,1423.4,1551.4,1840.8,2291.6,2662.0,2952.1,3118.4,3161.0,3224.1,3307.7,3238.6,3016.7,3094.5,3471.9,3742.6,3906.6,3945.4,3859.0,3689.9,3438.1,3192,2951.7,2684.8,2391.6,2144.9,1944.9,1761.3,1594.1,1475.8,1406.4,1353.9,1318.4,1295.6,1285.4,1280.3],"pv":[0,0,0,0,0,0,0,0,0,0,0,0,0,51.0,152.9,302.0,498.3,673.6,827.8,960.3,1070.9,1161.4,1231.5,1283.2,1316.2,1331.8,1330.0,1311.0,1274.7,1220.5,1148.2,1056.5,945.5,814.0,661.9,489.6,297.1,150.6,50.2,0,0,0,0,0,0,0,0,0],"rates":[0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.2723,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.3132,0.2723,0.2723,0.2723,0.2723],"co2_allow":0.5}   # a real Monday request sent by the page (Balanced strategy), rounded
for allow in (1.0, 0.5, 0.0):
    r = live.replan({**PAGE_DAY, "co2_allow": allow, "replay": {"now_step": 28, "scenario": NONE}}); e = r["expected"]
    ok(f"co2_allow {allow}: no deviation, re-plan costs no more than the day-ahead plan", e["new_plan"]["cost"] <= r["day_ahead_expected_cost"] + 0.5)
    ok(f"co2_allow {allow}: no deviation, re-plan is no worse than carrying on with the old plan", e["saving_rm"] >= -0.5)
    r = live.replan({**PAGE_DAY, "co2_allow": allow, "replay": {"now_step": 28, "scenario": CLOUD}})
    ok(f"co2_allow {allow}: cloud, re-plan is no worse than carrying on with the old plan", r["expected"]["saving_rm"] >= -0.5)

print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
