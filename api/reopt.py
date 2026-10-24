"""Scenario-triggered re-optimisation (rolling horizon) - a SIMULATION, not equipment control.

Question answered: the day-ahead plan was made from a forecast. Then the day happens differently (cloud, higher demand).
How much of the saving survives if the plan is only followed, and how much does re-planning during the day win back?

Inputs (same JSON as optimiser.optimise, which holds the FORECAST in load/pv) plus:
  actual    {"load": [...], "pv": [...]}   what really happens (same length), OR
  scenario  [{"what": "pv"|"load", "from_h": 12, "to_h": 15, "factor": 0.4}, ...]   simulated deviations applied to the forecast
  replan    {"every_steps": 4, "persistence_hours": 6, "trigger_kw": 150, "updates": [{"at_step": 24, "pv": [...], "load": [...]}]}
              every_steps  re-plan this often (0 = never on a timer)
              trigger_kw   re-plan early when the average gap between actual and expected net load since the last plan exceeds this (0 = off)
              updates      a new forecast that becomes available at a step (replaces the beliefs for steps from then on)
            With no update available, the plan is corrected by persistence: the rest of the day's forecast is scaled by
            the ratio actual / forecast seen over the last few steps (solar and demand separately, limited to 0..1.5), fading back to
              the forecast with time constant persistence_hours (default 6).

Strategies compared, all judged on the SAME actual day by the same KPI function (reference.kpis):
  solar_only, battery_self, battery_tou   reference strategies (reference.py)
  open_loop          follow the day-ahead plan unchanged; the grid absorbs every forecast error
  rolling            re-plan during the day, using only information available at that time
  perfect_foresight  optimise with the actual day known in advance: the best any plan could do (a bound, not achievable)

Executor rules (how a planned step is carried out on the real day): the battery and EV chargers do what the plan says, limited
by battery power and state of charge; the site load and solar are the actual values; solar that cannot be used is curtailed
(no export); the grid supplies the rest. Both open_loop and rolling get the same reactive limit guard (replan.guard, default
true): when import would pass the grid limit, planned grid charging is cancelled and the battery discharges harder. Without it,
following the plan would look worse than a real controller would be. Nothing is hard-coded: every figure comes from the time series.
"""
import math
import numpy as np
from optimiser import optimise, _steps, _vec
import reference as ref

MAX_REPLANS = 24


def _apply_scenario(load, pv, scenario, n):
    """Return (actual_load, actual_pv) = forecast with the listed deviations applied. Rejects nonsense."""
    al, ap = load.copy(), pv.copy()
    if not isinstance(scenario, list) or len(scenario) > 10:
        raise ValueError("scenario must be a list of at most 10 events")
    dt = 24.0 / n
    for e in scenario:
        if not isinstance(e, dict) or e.get("what") not in ("pv", "load"):
            raise ValueError("scenario event needs what = 'pv' or 'load'")
        try:
            a, b, f = float(e["from_h"]), float(e["to_h"]), float(e["factor"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("scenario event needs numeric from_h, to_h and factor")
        if not (math.isfinite(a) and math.isfinite(b) and math.isfinite(f)) or not 0 <= a < b <= 24 or not 0 <= f <= 3:
            raise ValueError("scenario event out of range (0 <= from_h < to_h <= 24, 0 <= factor <= 3)")
        idx = [t for t in range(n) if t * dt >= a - 1e-9 and (t + 1) * dt <= b + 1e-9]
        arr = ap if e["what"] == "pv" else al
        arr[idx] = arr[idx] * f
    return al, ap


def _parse(d):
    n = _steps(d)
    load, pv = _vec(d, "load", 0, 1e6, n), _vec(d, "pv", 0, 1e6, n)
    if d.get("actual") is not None:
        a = d["actual"]
        if not isinstance(a, dict):
            raise ValueError("actual must be an object with load and pv")
        al, ap = _vec(a, "load", 0, 1e6, n), _vec(a, "pv", 0, 1e6, n)
        desc = "actual series supplied"
    else:
        sc = d.get("scenario")
        if not sc:
            raise ValueError("send either actual {load, pv} or scenario [...]")
        al, ap = _apply_scenario(load, pv, sc, n)
        desc = "SIMULATED deviations applied to the forecast"
    rp = d.get("replan") or {}
    if not isinstance(rp, dict):
        raise ValueError("replan must be an object")
    every = int(rp.get("every_steps", 4))
    trig = float(rp.get("trigger_kw", 0.03 * float(load.max())))
    if not 0 <= every <= n or not math.isfinite(trig) or trig < 0:
        raise ValueError("replan.every_steps must be 0..steps and trigger_kw >= 0")
    ups = {}
    for u in rp.get("updates", []) or []:
        try:
            k = int(u["at_step"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("update needs at_step")
        if not 1 <= k < n:
            raise ValueError("update at_step must be between 1 and steps-1")
        ups[k] = {"load": _vec(u, "load", 0, 1e6, n) if u.get("load") is not None else None,
                  "pv": _vec(u, "pv", 0, 1e6, n) if u.get("pv") is not None else None}
    tau = float(rp.get("persistence_hours", 6.0))
    guard = bool(rp.get("guard", True))
    if not math.isfinite(tau) or not 0 <= tau <= 48:
        raise ValueError("replan.persistence_hours must be between 0 and 48")
    return n, load, pv, al, ap, every, trig, ups, tau, guard, desc


def _step(plan_ch, plan_dis, plan_flex, load, pv, soc, cap, eta, dt, limit=0.0):
    """Carry out one planned step on the real day. Returns grid, pv_used, ch, dis, flex, new_soc.
    Reactive limit guard (same for every executed strategy, when limit > 0): if grid import would exceed the limit, first
    cancel planned grid charging, then discharge the battery harder, within its power and charge. It reacts to what is
    happening now; it does not look ahead."""
    P = cap / 2.0
    ch = min(plan_ch, P, (cap - soc) / (eta * dt)) if cap > 0 else 0.0
    dis = min(plan_dis, P, soc * eta / dt) if cap > 0 else 0.0
    need = load + plan_flex + ch - dis
    if need < 0:                                  # no export: do not discharge more than the site can use
        dis += need
        need = 0.0
    g = need - min(pv, need)
    if limit > 0 and g > limit + 1e-9 and cap > 0:
        cut = min(ch, g - limit)                  # 1. stop charging from the grid
        ch -= cut; need -= cut; g -= cut
        if g > limit + 1e-9:                      # 2. discharge harder
            extra = max(0.0, min(g - limit, P - dis, soc * eta / dt - dis))
            dis += extra; need -= extra
    used = min(pv, need)
    return need - used, used, ch, dis, plan_flex, soc + eta * dt * ch - dt / eta * dis


def _ratio(act, fc, lo, hi, w=4):
    a, f = float(act[max(lo, hi - w):hi].sum()), float(fc[max(lo, hi - w):hi].sum())
    return 1.0 if f < 1e-6 else min(max(a / f, 0.0), 1.5)


def _persist(fc, ratio, t, dt, tau):
    """Scale fc[t:] by a correction that fades back to the original forecast with time constant tau hours
    (a deviation seen now is likely to last a while, not all day)."""
    out = fc.copy()
    k = np.arange(len(fc) - t) * dt
    out[t:] = fc[t:] * (1.0 + (ratio - 1.0) * np.exp(-k / tau)) if tau > 0 else fc[t:]
    return out


def _run(d, n, load, pv, al, ap, every, trig, ups, tau, rolling, guard=True):
    """Execute a day. rolling=False: follow the day-ahead plan. rolling=True: re-plan on timer / trigger / new forecast.
    Information rule: when re-planning before step t, only al[:t] and ap[:t] (the past) are used."""
    dt, cap = 24.0 / n, float(d.get("batt_kwh", 0))
    guard_limit = float(d.get("grid_limit", 0) or 0) if guard else 0.0
    eta = math.sqrt(float(d.get("eff", 0.90)))
    base = {k: v for k, v in d.items() if k not in ("actual", "scenario", "replan", "fixed")}
    plan = optimise(base)                                       # day-ahead plan, from the forecast only
    day_ahead_cost = plan["cost"]
    bl, bp = load.copy(), pv.copy()                             # what the planner currently believes about the day
    soc = 0.5 * cap
    ex = {k: [] for k in ("grid", "pv_used", "ch", "dis", "flex")}
    log, last, believed_net = [], 0, (bl - bp)
    for t in range(n):
        if rolling and t > 0 and len(log) < MAX_REPLANS:
            reason = None
            if t in ups:
                reason = "new forecast"
                for key, arr in (("load", bl), ("pv", bp)):
                    if ups[t][key] is not None:
                        arr[t:] = ups[t][key][t:]
            elif every and t - last >= every:
                reason = "scheduled"
            elif trig > 0 and t > last and float(np.abs((al[last:t] - ap[last:t]) - believed_net[last:t]).mean()) > trig:
                reason = "forecast error"
            if reason:
                if t not in ups:                                # persistence correction of the rest of the day
                    bl, bp = _persist(load, _ratio(al, load, last, t), t, dt, tau), _persist(pv, _ratio(ap, pv, last, t), t, dt, tau)
                req = dict(base, load=list(np.concatenate([al[:t], bl[t:]])), pv=list(np.concatenate([ap[:t], bp[t:]])),
                           fixed={k: ex[k][:t] for k in ("ch", "dis", "flex")})
                try:
                    plan = optimise(req)
                    log.append({"step": t, "hour": round(t * dt, 2), "reason": reason, "expected_cost": round(plan["cost"], 2),
                                "limit_ok": plan["limit_ok"]})
                except Exception as e:                          # keep the previous plan, say so
                    log.append({"step": t, "hour": round(t * dt, 2), "reason": reason, "failed": str(e)[:120]})
                believed_net = bl - bp
                believed_net[:t] = al[:t] - ap[:t]
                last = t
        r = _step(plan["ch"][t], plan["dis"][t], plan["flex"][t], al[t], ap[t], soc, cap, eta, dt, guard_limit)
        for key, v in zip(("grid", "pv_used", "ch", "dis", "flex"), r[:5]):
            ex[key].append(float(v))
        soc = r[5]
    return plan, ex, log, day_ahead_cost, soc


def simulate(d):
    n, load, pv, al, ap, every, trig, ups, tau, guard, desc = _parse(d)
    base = {k: v for k, v in d.items() if k not in ("actual", "scenario", "replan", "fixed")}
    actual_d = dict(base, load=list(al), pv=list(ap))
    ev_unmet = ref._ev_unmet(base)

    def judge(ex, tl):
        return ref.kpis(actual_d, ex["grid"], ex["pv_used"], ex["ch"], ex["dis"], tl, ev_unmet)

    res = {}
    for name, fn in (("solar_only", lambda: ref.solar_only(actual_d)),
                     ("battery_self", lambda: ref.battery_rule(actual_d, "self")),
                     ("battery_tou", lambda: ref.battery_rule(actual_d, "tou"))):
        r = fn()
        res[name] = {k: v for k, v in r.items() if k not in ("grid", "pv_used", "ch", "dis", "load")}
    series = {"actual_load": list(al), "actual_pv": list(ap), "forecast_load": list(load), "forecast_pv": list(pv)}
    for name, rolling in (("open_loop", False), ("rolling", True)):
        plan, ex, log, day_ahead_cost, soc = _run(base, n, load, pv, al, ap, every, trig, ups, tau, rolling, guard)
        tl = np.array(al) + np.array(ex["flex"])
        res[name] = judge(ex, tl)
        res[name]["soc_end_kwh"] = soc
        series[name + "_grid"] = ex["grid"]
        if rolling:
            replans = log
        else:
            day_ahead = day_ahead_cost
    pf = ref.optimised(actual_d)
    res["perfect_foresight"] = {k: v for k, v in pf.items() if k not in ("grid", "pv_used", "ch", "dis", "load", "flex")}
    series["perfect_foresight_grid"] = pf["grid"]
    best = min(("solar_only", "battery_self", "battery_tou"), key=lambda k: res[k]["cost"])
    pct = ref.pct
    return {
        "steps": n,
        "data_kind": "SIMULATION: " + desc + ". Forecast inputs are model output; no equipment is controlled.",
        "day_ahead_expected_cost": day_ahead,
        "results": res,
        "replans": replans,
        "best_reference": best,
        "summary": {
            "open_loop_vs_best_reference_pct": pct(res[best]["cost"], res["open_loop"]["cost"]),
            "rolling_vs_best_reference_pct": pct(res[best]["cost"], res["rolling"]["cost"]),
            "perfect_vs_best_reference_pct": pct(res[best]["cost"], res["perfect_foresight"]["cost"]),
            "rolling_vs_open_loop_rm": res["open_loop"]["cost"] - res["rolling"]["cost"],
            "forecast_error_cost_rm": res["open_loop"]["cost"] - day_ahead,
            "value_recovered_pct_of_gap": (100.0 * (res["open_loop"]["cost"] - res["rolling"]["cost"]) /
                                           (res["open_loop"]["cost"] - res["perfect_foresight"]["cost"])
                                           if res["open_loop"]["cost"] - res["perfect_foresight"]["cost"] > 1.0 else None),
        },
        "series": series,
        "notes": ["Rolling re-planning uses only information available at the time (past actuals plus a persistence-corrected or newly supplied forecast).",
                  "Perfect foresight knows the whole actual day in advance: it is a bound, not something a real controller can reach.",
                  "No export income and no real-time equipment control are modelled."],
    }
