"""Today so far: compare the forecast with what has really happened, correct the rest of the day, and re-plan from now.
A RECOMMENDATION workflow. Nothing here controls equipment.

Request = the usual optimiser request (forecast load and pv for the WHOLE day, rates, battery, limit, EV block ...) plus ONE of:

  now     MEASURED data for the steps already passed (step 0 = 00:00 today, k steps, 1 <= k < steps):
            {"load": [kW]*k, "pv": [kW]*k,                       required
             "battery_kw": [kW]*k   (discharge +, charging -),    optional, else the day-ahead plan is ASSUMED followed
             "ev_kw": [kW]*k,                                     optional, else the day-ahead plan is ASSUMED followed
             "grid_kw": [kW]*k,                                   optional, only used to check the data against the energy balance
             "soc_kwh": number}                                   optional measured battery charge now, else computed from 50% at 00:00
  replay  {"now_step": k, "scenario": [...]}   SIMULATED replay for demos: the day is the forecast with the listed deviations
          (same format as reopt.py), cut at step k. Labelled SIMULATED in the output.

What it does
  1. Forecast error over the elapsed steps (mean absolute error, bias, actual/forecast ratio) for load and solar.
  2. Corrects the rest of the day's forecast by persistence of the recent ratio, fading back to the forecast (persistence_hours, default 6).
  2b. If a learned profile is sent (learned = see learn.py), the forecast is first multiplied by the ratio learned for its time of day.
  3. Re-plans steps k.. with the elapsed steps locked to what happened (and the battery charge locked to the measurement if given).
  4. Compares, on the SAME corrected forecast, continuing the old day-ahead plan against the new plan: expected cost for the whole day.
Expected costs are estimates: the rest of the day is still a forecast.
"""
import math
import numpy as np
from optimiser import optimise, _steps, _vec, _num
import reference as ref
import reopt
import learn


def _series(src, key, k, lo, hi, required=False):
    v = src.get(key)
    if v is None:
        if required:
            raise ValueError(f"now.{key} is required")
        return None
    if not isinstance(v, list) or len(v) != k:
        raise ValueError(f"now.{key} must be a list of {k} numbers (one per elapsed step)")
    try:
        a = np.array([float(x) for x in v])
    except (TypeError, ValueError):
        raise ValueError(f"now.{key} must contain only numbers")
    if not np.all(np.isfinite(a)) or a.min() < lo or a.max() > hi:
        raise ValueError(f"now.{key} has a value outside {lo} to {hi}")
    return a


def _stats(act, fc):
    err = act - fc
    s_fc, s_act = float(fc.sum()), float(act.sum())
    return {"mae_kw": float(np.abs(err).mean()), "bias_kw": float(err.mean()),
            "ratio": (s_act / s_fc) if s_fc > 1e-6 else None,
            "mae_pct_of_mean": (100.0 * float(np.abs(err).mean()) / (s_fc / len(fc))) if s_fc > 1e-6 else None}


def replan(d):
    n = _steps(d)
    dt = 24.0 / n
    load, pv = _vec(d, "load", 0, 1e6, n), _vec(d, "pv", 0, 1e6, n)
    cap = _num(d, "batt_kwh", 0, 20000)
    limit = _num(d, "grid_limit", 0, 1e6)
    eta = math.sqrt(float(d.get("eff", 0.90)))
    tau = float(d.get("persistence_hours", 6.0))
    if not math.isfinite(tau) or not 0 <= tau <= 48:
        raise ValueError("persistence_hours must be between 0 and 48")
    raw_load, raw_pv = load, pv
    learned = d.get("learned")
    if learned:                                                     # forecast corrected by what was learned on earlier days
        load, pv = learn.apply(learned, load, pv)
    base = {k: v for k, v in d.items() if k not in ("now", "replay", "fixed", "soc_now_kwh", "scenario", "replan", "learned")}
    base["load"], base["pv"] = list(load), list(pv)

    plan0 = optimise(base)                                         # the day-ahead plan, from the forecast only
    sim = d.get("replay") is not None
    bat = ev = gm = soc_meas = None
    if sim:
        rp = d["replay"]
        if not isinstance(rp, dict):
            raise ValueError("replay must be an object")
        try:
            k = int(rp["now_step"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("replay.now_step is required")
        if not 1 <= k < n:
            raise ValueError(f"replay.now_step must be between 1 and {n - 1}")
        al_all, ap_all = reopt._apply_scenario(raw_load, raw_pv, rp.get("scenario") or [], n)   # the real day comes from the RAW forecast
        al, ap = al_all[:k], ap_all[:k]
        kind = "SIMULATED REPLAY: the day so far is the forecast with simulated deviations. It is not measured data."
    else:
        now = d.get("now")
        if not isinstance(now, dict):
            raise ValueError("send now {load, pv, ...} with measured data, or replay {now_step, scenario}")
        lst = now.get("load")
        if not isinstance(lst, list) or not 1 <= len(lst) < n:
            raise ValueError(f"now.load must be a list of 1 to {n - 1} numbers (steps since 00:00)")
        k = len(lst)
        al, ap = _series(now, "load", k, 0, 1e6, True), _series(now, "pv", k, 0, 1e6, True)
        bat, ev, gm = _series(now, "battery_kw", k, -1e5, 1e5), _series(now, "ev_kw", k, 0, 1e5), _series(now, "grid_kw", k, -1e6, 1e6)
        if now.get("soc_kwh") is not None:
            try:
                soc_meas = float(now["soc_kwh"])
            except (TypeError, ValueError):
                raise ValueError("now.soc_kwh must be a number")
            if not math.isfinite(soc_meas) or not 0 <= soc_meas <= cap + 1e-6:
                raise ValueError("now.soc_kwh must be between 0 and the battery size")
        kind = "MEASURED data for the steps already passed (supplied by you); the rest of the day is a corrected forecast."

    # ---- 1. what happened in the elapsed steps (decisions are measured or assumed; grid and solar use follow from the balance)
    soc = 0.5 * cap
    ch, dis, fl, g, u = (np.zeros(k) for _ in range(5))
    for t in range(k):
        f = float(ev[t]) if ev is not None else float(plan0["flex"][t])
        if bat is None:                                            # no battery reading: assume the day-ahead plan was followed
            g[t], u[t], ch[t], dis[t], fl[t], soc = reopt._step(plan0["ch"][t], plan0["dis"][t], f, al[t], ap[t], soc, cap, eta, dt, limit)
        else:
            c, x = max(-bat[t], 0.0), max(bat[t], 0.0)
            need = al[t] + f + c - x
            if need < 0:
                x += need
                need = 0.0
            u[t] = min(ap[t], need)
            g[t], ch[t], dis[t], fl[t] = need - u[t], c, x, f
            soc += eta * dt * c - dt / eta * x
    soc_k = soc_meas if soc_meas is not None else soc

    # ---- 2. forecast error and corrected belief for the rest of the day
    rl, rp_ = reopt._ratio(al, load, 0, k), reopt._ratio(ap, pv, 0, k)
    bl, bp = reopt._persist(load, rl, k, dt, tau), reopt._persist(pv, rp_, k, dt, tau)
    L, P = np.concatenate([al, bl[k:]]), np.concatenate([ap, bp[k:]])
    cur_d = dict(base, load=list(L), pv=list(P))

    # ---- 3. re-plan from now, elapsed steps locked
    req = dict(cur_d, fixed={"ch": list(ch), "dis": list(dis), "flex": list(fl)})
    if soc_meas is not None:
        req["soc_now_kwh"] = soc_meas
    try:
        new = optimise(req)
    except RuntimeError as e:
        raise ValueError("could not re-plan from this point: " + str(e)[:160])

    # ---- 4. continue the OLD plan on the same corrected forecast, for a like-for-like comparison
    og, ou, oc, ox, of = list(g), list(u), list(ch), list(dis), list(fl)
    s = soc_k
    for t in range(k, n):
        r = reopt._step(plan0["ch"][t], plan0["dis"][t], plan0["flex"][t], L[t], P[t], s, cap, eta, dt, limit)
        og.append(r[0]); ou.append(r[1]); oc.append(r[2]); ox.append(r[3]); of.append(r[4]); s = r[5]
    ev_unmet = float(plan0.get("ev_unmet_kwh", 0.0))
    old_k = ref.kpis(cur_d, og, ou, oc, ox, L + np.array(of), ev_unmet)
    new_k = ref.kpis(cur_d, new["grid"], new["pv_used"], new["ch"], new["dis"], np.array(new["ld"]), ev_unmet)
    strip = lambda x: {a: b for a, b in x.items() if not isinstance(b, list)}

    # ---- what has been spent so far
    _, _, _, _, rate, md_rate, _, _, _, mdh = ref._inputs(cur_d)
    spent = float(rate[:k] @ g) * dt
    peak_so_far = float(max([g[t] for t in mdh if t < k], default=0.0))

    # ---- data quality: measured grid import against the balance of the other readings
    dq = {"grid_checked": gm is not None}
    if gm is not None:
        gap = float(np.abs(gm - g).mean())
        dq.update(grid_gap_kw=gap, grid_gap_pct_of_load=100.0 * gap / max(float(al.mean()), 1e-6),
                  warn=bool(gap > 0.05 * max(float(al.mean()), 1e-6)))
    dq["battery_source"] = "measured" if bat is not None else "assumed: the day-ahead plan was followed"
    dq["ev_source"] = "measured" if ev is not None else "assumed: the day-ahead plan was followed"
    dq["soc_source"] = "measured" if soc_meas is not None else "computed from 50% at 00:00"

    nxt = [{"step": t, "hour": round(t * dt, 2), "battery_kw": new["dis"][t] - new["ch"][t], "ev_kw": new["flex"][t], "grid_kw": new["grid"][t]}
           for t in range(k, min(n, k + 8))]
    return {
        "steps": n, "now_step": k, "now_hour": k * dt, "data_kind": kind, "simulated": sim, "data_quality": dq,
        "forecast_error": {"load": _stats(al, load[:k]), "pv": _stats(ap, pv[:k])},
        "forecast_error_raw": {"load": _stats(al, raw_load[:k]), "pv": _stats(ap, raw_pv[:k])},
        "learned": {"applied": bool(learned), "days": int(learned["days"]) if learned else 0},
        "correction": {"load_ratio": rl, "pv_ratio": rp_, "fade_hours": tau},
        "spent_so_far_rm": spent, "peak_so_far_kw": peak_so_far,
        "day_ahead_expected_cost": plan0["cost"],
        "expected": {"old_plan": strip(old_k), "new_plan": strip(new_k),
                     "saving_rm": old_k["cost"] - new_k["cost"],
                     "saving_pct": ref.pct(old_k["cost"], new_k["cost"])},
        "next_steps": nxt,
        "series": {"forecast_load": list(load), "forecast_pv": list(pv), "actual_load": list(al), "actual_pv": list(ap),
                   "belief_load": list(bl), "belief_pv": list(bp), "new_grid": new["grid"], "new_ch": new["ch"], "new_dis": new["dis"],
                   "new_flex": new["flex"], "new_pv_used": new["pv_used"], "new_ld": new["ld"], "old_grid": og},
        "notes": ["Recommendation only: no equipment is controlled.",
                  "Expected costs use the corrected forecast for the rest of the day, so they are estimates, not measurements.",
                  "Old plan and new plan are compared on the same corrected forecast, from the same state, with the same grid-limit guard."],
    }
