"""Checks for learn.py (error learning per time of day) and its use in live.py. Run:  python test_learn.py"""
import math
import numpy as np
import learn, live
from test_reference_data import make

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))
D = make()
LF, PF = np.array(D["load"]), np.array(D["pv"])

# 1. update rule
p = learn.new_profile(48)
ok("a new profile is neutral (all ratios 1, 0 days)", p["days"] == 0 and set(p["pv"]) == {1.0} and set(p["load"]) == {1.0})
u = learn.update(p, LF, PF, LF * 1.10, PF * 0.80)["profile"]
day_slots = [i for i in range(48) if PF[i] > 0.02 * PF.max() + 1e-9]
ok("one day: solar ratio moves to the observed 0.8 on daylight slots", all(abs(u["pv"][i] - 0.8) < 0.03 for i in day_slots[2:-2]))
ok("one day: load ratio moves to the observed 1.1", all(abs(u["load"][i] - 1.1) < 1e-6 for i in range(48)))
ok("night slots (no solar forecast) are not changed", all(u["pv"][i] == 1.0 and u["pv_count"][i] == 0 for i in range(48) if PF[i] <= 1.0))
ok("day counter increases and the old profile is not modified", u["days"] == 1 and p["days"] == 0 and p["pv"][20] == 1.0)
u2 = learn.update(u, LF, PF, LF * 1.10, PF * 0.60, alpha=0.15)["profile"]
mid = day_slots[len(day_slots) // 2]
ok("second day: plain average first (0.8 and 0.6 -> about 0.7)", abs(u2["pv"][mid] - 0.7) < 0.02)
big = u
for _ in range(60):
    big = learn.update(big, LF, PF, LF * 5.0, PF * 0.01)["profile"]
ok("ratios stay inside the allowed range (0.2 to 3)", min(big["pv"]) >= 0.2 and max(big["load"]) <= 3.0)
a = learn.new_profile(48); b = learn.new_profile(48)
for r in [1.0] * 30 + [0.5] * 3:
    a = learn.update(a, LF, PF, LF, PF * r, alpha=0.3)["profile"]
for r in [1.0] * 3 + [0.5] * 30:
    b = learn.update(b, LF, PF, LF, PF * r, alpha=0.3)["profile"]
ok("recent days weigh more than old ones: 30 days at 1.0 then 3 days at 0.5 gives well below the plain average of 0.95", a["pv"][mid] < 0.75)
ok("a long run of 0.5 after 3 days of 1.0 ends near 0.5", abs(b["pv"][mid] - 0.5) < 0.03)

# 2. apply
Lc, Pc = learn.apply(u, LF, PF)
ok("apply multiplies forecast by the slot ratio", abs(Lc[10] - LF[10] * u["load"][10]) < 1e-9 and abs(Pc[mid] - PF[mid] * u["pv"][mid]) < 1e-9)
ok("zero solar stays zero", all(Pc[i] == 0 for i in range(48) if PF[i] == 0))

# 3. backtest: evidence and honesty
bt = learn.backtest(D, days=30, bias=True)
s = bt["summary"]
ok("backtest is labelled SIMULATED", "SIMULATED" in bt["data_kind"])
ok("with a systematic error, solar error in the last third is less than 60% of the raw error", s["last_pv_mae_learned_kw"] < 0.6 * s["last_pv_mae_raw_kw"])
ok("with a systematic error, load error in the last third is less than 75% of the raw error", s["last_load_mae_learned_kw"] < 0.75 * s["last_load_mae_raw_kw"])
ok("with a systematic error, the real cost over 30 days is lower with learning", s["total_cost_learned_rm"] < s["total_cost_raw_rm"])
ok("the learned ratios end close to the injected bias", s["learned_vs_true_bias_pv"] < 0.06 and s["learned_vs_true_bias_load"] < 0.03)
nb = learn.backtest(D, days=30, bias=False)["summary"]
ok("control, no systematic error: learning does not make solar error more than 10% worse", nb["last_pv_mae_learned_kw"] < 1.10 * nb["last_pv_mae_raw_kw"])
ok("control, no systematic error: cost within 0.5% of the raw plan", abs(nb["total_cost_learned_rm"] - nb["total_cost_raw_rm"]) < 0.005 * nb["total_cost_raw_rm"])
ok("day 1 has no learning: learned error = raw error", bt["days"][0]["profile_days"] == 0 and abs(bt["days"][0]["pv_mae_raw_kw"] - bt["days"][0]["pv_mae_learned_kw"]) < 1e-9)

# 4. no leakage: the profile used on day k comes from days before k only
rows = bt["days"]
ok("profile used on day k has learned exactly k-1 days", all(r["profile_days"] == r["day"] - 1 for r in rows))
a1 = learn.backtest(D, days=12, bias=True, seed=3)["days"]
a2 = learn.backtest(D, days=20, bias=True, seed=3)["days"]
ok("changing LATER days does not change the result on earlier days (same seed, longer run)", all(abs(a1[i]["pv_mae_learned_kw"] - a2[i]["pv_mae_learned_kw"]) < 1e-9 and abs(a1[i]["cost_learned_rm"] - a2[i]["cost_learned_rm"]) < 1e-6 for i in range(12)))

# 4b. rebuild the profile independently from the days before k and compare with what the backtest used on day k
dbg = learn.backtest(D, days=10, bias=True, seed=11, debug=True)["days"]
rp, same = None, True
for k_, r in enumerate(dbg):
    if k_ == 0:
        same &= r["_used_pv"] is None
    else:
        same &= np.allclose(r["_used_pv"], rp["pv"], atol=1e-12) and np.allclose(r["_used_load"], rp["load"], atol=1e-12)
    rp = learn.update(rp, LF, PF, r["_al"], r["_ap"])["profile"]
ok("independent rebuild: the profile used on every day equals one built from the previous days only", same)

# 5. use in the live re-plan
cl = [{"what": "pv", "from_h": 12, "to_h": 24, "factor": 0.8}]
prof = learn.new_profile(48)
for _ in range(10):
    prof = learn.update(prof, LF, PF, LF, PF * 0.8)["profile"]
r0 = live.replan({**D, "replay": {"now_step": 28, "scenario": [{"what": "pv", "from_h": 0, "to_h": 24, "factor": 0.8}]}})
r1 = live.replan({**D, "learned": prof, "replay": {"now_step": 28, "scenario": [{"what": "pv", "from_h": 0, "to_h": 24, "factor": 0.8}]}})
ok("live: without a profile nothing is applied", r0["learned"]["applied"] is False)
ok("live: with a profile it is applied and counted", r1["learned"]["applied"] and r1["learned"]["days"] == 10)
ok("live: learned forecast has a smaller solar error than the raw forecast", r1["forecast_error"]["pv"]["mae_kw"] < 0.4 * r1["forecast_error_raw"]["pv"]["mae_kw"])
ok("live: the raw forecast error is still reported", abs(r1["forecast_error_raw"]["pv"]["mae_kw"] - r0["forecast_error"]["pv"]["mae_kw"]) < 1e-6)

# 6. bad input
for bad, why in (({**prof, "n": 24}, "profile for the wrong number of steps"), ({**prof, "pv": [1.0] * 10}, "short profile list"),
                 ({**prof, "pv": [9.0] * 48}, "ratio of 9"), ({**prof, "load": ["x"] * 48}, "text in profile"), ({**prof, "days": -1}, "negative days"), ("hello", "profile that is not an object")):
    try:
        live.replan({**D, "learned": bad, "replay": {"now_step": 28, "scenario": cl}}); ok("rejected: " + why, False)
    except ValueError:
        ok("rejected: " + why, True)
for args, why in (((None, LF[:10], PF[:10], LF[:10], PF[:10]), "10 values"), ((None, LF, PF, LF, PF * -1), "negative actual"), ((None, LF, PF, LF, PF, 0.9), "alpha 0.9")):
    try:
        learn.update(*args); ok("rejected: update with " + why, False)
    except ValueError:
        ok("rejected: update with " + why, True)
for kw, why in (({"days": 2}, "2 days"), ({"days": 500}, "500 days"), ({"days": "x"}, "text days")):
    try:
        learn.backtest(D, **kw); ok("rejected: backtest with " + why, False)
    except ValueError:
        ok("rejected: backtest with " + why, True)

print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
