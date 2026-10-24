"""Learning from forecast error, one correction ratio per time of day (stateless: the caller keeps the profile).

Idea: a forecast is often wrong in the SAME way at the same time of day (for example afternoon cloud that the weather model misses,
or an evening load the demand model underestimates). Each half-hour slot keeps a ratio  actual / forecast  that is updated after every
full day of data with an exponentially weighted average, so slow, repeatable errors are learned and one-off weather noise is averaged out.

  profile = {"n": 48, "days": 12, "pv": [48 ratios], "load": [48 ratios], "pv_count": [48 ints], "load_count": [48 ints]}

  update(profile, forecast, actual, alpha)   one full day -> new profile
  apply(profile, load, pv)                   corrected forecast = forecast x ratio of its slot
  backtest(request, days, bias, seed)        SIMULATED multi-day demo/evidence, no peeking at the future

Honest limits: a ratio cannot fix errors that change from day to day, it needs full days of data, and the backtest injects its own
systematic bias, so it demonstrates the mechanism; it does not measure real-world gain.
"""
import math
import numpy as np
from optimiser import optimise, _steps, _vec
import reference as ref
import reopt

CLIP = (0.3, 2.0)          # one day's ratio is limited to this range before it is learned
PROFILE_RANGE = (0.2, 3.0)


def new_profile(n):
    return {"n": n, "days": 0, "pv": [1.0] * n, "load": [1.0] * n, "pv_count": [0] * n, "load_count": [0] * n}


def check_profile(p, n):
    """Validate a profile that came from the browser. Returns it unchanged or raises ValueError."""
    if not isinstance(p, dict):
        raise ValueError("learned profile must be an object")
    if p.get("n") != n:
        raise ValueError(f"learned profile is for {p.get('n')} steps per day, this request has {n}")
    for key in ("pv", "load", "pv_count", "load_count"):
        v = p.get(key)
        if not isinstance(v, list) or len(v) != n:
            raise ValueError(f"learned profile {key} must be a list of {n} numbers")
        try:
            a = np.array([float(x) for x in v])
        except (TypeError, ValueError):
            raise ValueError(f"learned profile {key} must contain numbers")
        lo, hi = (PROFILE_RANGE if key in ("pv", "load") else (0, 1e6))
        if not np.all(np.isfinite(a)) or a.min() < lo or a.max() > hi:
            raise ValueError(f"learned profile {key} has a value outside {lo} to {hi}")
    d = p.get("days")
    if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 100000:
        raise ValueError("learned profile days must be a whole number")
    return p


def _day_ratio(fc, act, min_fc):
    """actual/forecast per slot, NaN where the forecast is too small to say anything, lightly smoothed over neighbouring slots."""
    r = np.full(len(fc), np.nan)
    m = fc > min_fc
    r[m] = np.clip(act[m] / fc[m], *CLIP)
    s = r.copy()
    for i in range(len(r)):
        if np.isnan(r[i]):
            continue
        w, tot = 0.5, 0.5 * r[i]
        for j in (i - 1, i + 1):
            if 0 <= j < len(r) and not np.isnan(r[j]):
                w += 0.25
                tot += 0.25 * r[j]
        s[i] = tot / w
    return s


def update(profile, load_fc, pv_fc, load_act, pv_act, alpha=0.15):
    """One full day of forecast and measurement -> new profile (the old one is not modified)."""
    n = len(load_fc)
    if not all(len(a) == n for a in (pv_fc, load_act, pv_act)) or n not in (24, 48):
        raise ValueError("send 24 or 48 values for forecast and actual, load and solar")
    if not 0.01 <= alpha <= 0.5:
        raise ValueError("alpha must be between 0.01 and 0.5")
    p = check_profile(profile, n) if profile else new_profile(n)
    lf, pf, la, pa = (np.array(a, dtype=float) for a in (load_fc, pv_fc, load_act, pv_act))
    if not all(np.all(np.isfinite(a)) and a.min() >= 0 for a in (lf, pf, la, pa)):
        raise ValueError("forecast and actual values must be finite and not negative")
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in p.items()}
    changed = {}
    for key, fc, act, floor in (("pv", pf, pa, max(1.0, 0.02 * float(pf.max()))), ("load", lf, la, max(1.0, 0.02 * float(lf.max())))):
        r = _day_ratio(fc, act, floor)
        cnt, rat, k = out[key + "_count"], out[key], 0
        for i in range(n):
            if np.isnan(r[i]):
                continue
            cnt[i] += 1
            a = max(alpha, 1.0 / cnt[i])                   # first days: plain average, then exponential weighting
            rat[i] = float(np.clip((1 - a) * rat[i] + a * r[i], *PROFILE_RANGE))
            k += 1
        changed[key] = k
    out["days"] = int(p["days"]) + 1
    return {"profile": out, "slots_updated": changed}


def apply(profile, load, pv):
    """Forecast x learned ratio of the same time of day. Solar that is zero stays zero."""
    n = len(load)
    p = check_profile(profile, n)
    return np.asarray(load, dtype=float) * np.array(p["load"]), np.asarray(pv, dtype=float) * np.array(p["pv"])


# ---------------------------------------------------------------- SIMULATED backtest
def _true_bias(n, on):
    h = (np.arange(n) + 0.5) * 24.0 / n
    pv = np.ones(n)
    ld = np.ones(n)
    if on:
        pv = pv - 0.25 * ((h >= 12) & (h < 18)) + 0.05 * ((h >= 7) & (h < 12))          # weather model too sunny in the afternoon
        ld = ld + 0.08 * ((h >= 17) & (h < 22)) - 0.03 * ((h >= 10) & (h < 15))         # demand model misses the evening
    return pv, ld


def _day_cost(d, load_plan, pv_plan, al, ap):
    """Plan on (load_plan, pv_plan), live the day (al, ap) following the plan, return the real cost."""
    n = _steps(d)
    base = dict(d, load=list(load_plan), pv=list(pv_plan))
    _, ex, _, _, _ = reopt._run(base, n, np.asarray(load_plan), np.asarray(pv_plan), al, ap, 0, 0.0, {}, 6.0, False, True)
    actual_d = dict(d, load=list(al), pv=list(ap))
    tl = al + np.array(ex["flex"])
    return ref.kpis(actual_d, ex["grid"], ex["pv_used"], ex["ch"], ex["dis"], tl, ref._ev_unmet(d))["cost"]


def backtest(d, days=30, bias=True, seed=7, alpha=0.15, debug=False):
    """SIMULATED: the same forecast shape every day, a hidden systematic error (if bias) plus random cloud and load noise. Day k is
    corrected with a profile learned from days 0..k-1 ONLY. Compares forecast error and the real cost of the plan made from the
    raw forecast against the plan made from the corrected forecast."""
    if not isinstance(days, int) or isinstance(days, bool) or not 5 <= days <= 60:
        raise ValueError("days must be a whole number from 5 to 60")
    n = _steps(d)
    F_l, F_p = _vec(d, "load", 0, 1e6, n), _vec(d, "pv", 0, 1e6, n)
    b_pv, b_ld = _true_bias(n, bool(bias))
    rng = np.random.default_rng(int(seed))
    block = np.minimum((np.arange(n) * 24 // n) // 3, 7)
    prof, rows = None, []
    peak_pv = max(float(F_p.max()), 1.0)
    for day in range(days):
        cloud = np.clip(rng.normal(1.0, 0.12, 8), 0.5, 1.2)[block]
        ap = F_p * b_pv * cloud
        al = F_l * b_ld * (1 + rng.normal(0, 0.03)) * (1 + rng.normal(0, 0.01, n))
        Lc, Pc = (apply(prof, F_l, F_p) if prof else (F_l, F_p))
        mae = lambda a, b: float(np.abs(a - b).mean())
        rows.append({"day": day + 1, "profile_days": prof["days"] if prof else 0,
                     "pv_mae_raw_kw": mae(F_p, ap), "pv_mae_learned_kw": mae(Pc, ap),
                     "load_mae_raw_kw": mae(F_l, al), "load_mae_learned_kw": mae(Lc, al),
                     "cost_raw_rm": _day_cost(d, F_l, F_p, al, ap), "cost_learned_rm": _day_cost(d, Lc, Pc, al, ap)})
        if debug:                                                          # for the no-peeking test only
            rows[-1].update(_al=list(al), _ap=list(ap), _used_pv=list(prof["pv"]) if prof else None, _used_load=list(prof["load"]) if prof else None)
        prof = update(prof, F_l, F_p, al, ap, alpha)["profile"]            # learn AFTER the day is over
    k = max(1, days // 3)
    avg = lambda key, a, b: float(np.mean([r[key] for r in rows[a:b]]))
    first, last = (0, k), (days - k, days)
    summ = {"days": days, "bias_injected": bool(bias), "window_days": k}
    for key in ("pv_mae_raw_kw", "pv_mae_learned_kw", "load_mae_raw_kw", "load_mae_learned_kw", "cost_raw_rm", "cost_learned_rm"):
        summ["first_" + key] = avg(key, *first)
        summ["last_" + key] = avg(key, *last)
    summ["total_cost_raw_rm"] = float(sum(r["cost_raw_rm"] for r in rows))
    summ["total_cost_learned_rm"] = float(sum(r["cost_learned_rm"] for r in rows))
    summ["total_saving_rm"] = summ["total_cost_raw_rm"] - summ["total_cost_learned_rm"]
    summ["total_saving_pct"] = ref.pct(summ["total_cost_raw_rm"], summ["total_cost_learned_rm"])
    summ["learned_vs_true_bias_pv"] = float(np.abs(np.array(prof["pv"]) - b_pv)[F_p > 0.02 * peak_pv].mean())
    summ["learned_vs_true_bias_load"] = float(np.abs(np.array(prof["load"]) - b_ld).mean())
    return {"data_kind": "SIMULATED: the forecast error pattern is injected by the test, not measured. It shows the mechanism, not real-world gain.",
            "summary": summ, "days": rows, "profile": prof,
            "true_bias": {"pv": list(b_pv), "load": list(b_ld)},
            "notes": ["Day k is corrected using days before k only (no peeking).", "Random cloud and load noise cannot be learned and stays in the error."]}
