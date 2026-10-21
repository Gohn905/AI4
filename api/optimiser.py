"""Hourly cost optimiser for a facility with solar, battery and shiftable load.

Pure function: tariff logic stays in the dashboard, which sends hourly rates and
the hours that count for maximum demand. Returns the cheapest schedule.
Linear program solved with HiGHS (scipy).
Extra inputs: eff (battery round-trip efficiency, default 0.90), grid_kwh_cap (optional cap on the day's total grid energy,
soft: going over it is allowed but very costly, so the problem is never infeasible), and co2_allow (0 to 1): the share of the
extra grid energy that the cheapest plan would add on top of the solar-only baseline that this plan may add.
co2_allow 1 = no cap (cheapest plan), 0.5 = half of it, 0 = no extra grid energy (and so no extra CO2) at all. Diesel on/off decisions (binary) live
in mems_milp.py and can be added here later as a MILP.
"""
import math
import numpy as np
from scipy.optimize import linprog

N = 24


def _vec(d, key, lo, hi):
    v = d.get(key)
    if not isinstance(v, list) or len(v) != N:
        raise ValueError(f"{key} must be a list of {N} numbers")
    a = np.array(v, dtype=float)
    if not np.all(np.isfinite(a)) or a.min() < lo or a.max() > hi:
        raise ValueError(f"{key} out of range")
    return a


def _num(d, key, lo, hi):
    x = float(d.get(key, 0))
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return x


def optimise(d):
    load = _vec(d, "load", 0, 1e6)           # kW, original hourly load
    pv = _vec(d, "pv", 0, 1e6)               # kW, available solar
    rate = _vec(d, "rates", 0, 10)           # RM/kWh incl. adders and AFA
    md_rate = _num(d, "md_rate_day", 0, 1000)  # RM/kW/day
    cap = _num(d, "batt_kwh", 0, 20000)
    share = _num(d, "flex_share", 0, 0.5)
    carbon = _num(d, "carbon_adder", 0, 10)   # RM/kWh of grid energy (carbon price x emission factor)
    limit = _num(d, "grid_limit", 0, 1e6)    # kW cap on grid import in ANY hour, 0 = none
    wear = _num(d, "wear", 0, 10)             # RM per kWh of battery throughput
    eff = float(d.get("eff", 0.90))
    if not math.isfinite(eff) or not 0.5 <= eff <= 1.0:
        raise ValueError("eff must be between 0.5 and 1.0")
    eta = math.sqrt(eff)
    allow = d.get("co2_allow")
    if allow is not None:
        allow = float(allow)
        if not math.isfinite(allow) or not 0 <= allow <= 1:
            raise ValueError("co2_allow must be between 0 and 1")
    gcap = float(d.get("grid_kwh_cap", 0) or 0)
    if not math.isfinite(gcap) or gcap < 0:
        raise ValueError("grid_kwh_cap must be zero or more")
    if allow is not None and allow < 1 and gcap == 0:
        free = optimise({**d, "co2_allow": 1})                   # the cheapest plan, no cap
        base = float(np.maximum(load - pv, 0).sum())             # solar-only baseline grid energy
        extra = float(sum(free["grid"])) - base
        if extra <= 0.5:
            return free                                         # nothing extra to hold back
        return optimise({**d, "co2_allow": 1, "grid_kwh_cap": base + allow * extra})
    md_hours = [int(h) for h in d.get("md_hours", []) if 0 <= int(h) < N]
    P = cap / 2.0
    win = list(range(9, 17))                  # shiftable only 09:00-17:00

    fixed = load.copy()
    flex_total = share * load[win].sum()
    fixed[win] = load[win] * (1 - share)
    cp = 1.5 * flex_total / len(win)

    ix, k = {}, 0
    for name, size in [("Imp", N), ("MD", 1), ("Ch", N), ("Dis", N),
                       ("SOC", N + 1), ("Flex", N), ("PVu", N), ("Over", N), ("MDo", 1), ("CapO", 1)]:
        ix[name] = np.arange(k, k + size); k += size
    nv = k
    c = np.zeros(nv); c[ix["Imp"]] = rate + carbon; c[ix["MD"]] = md_rate
    c[ix["Ch"]] = wear; c[ix["Dis"]] = wear
    c[ix["Over"]] = 50.0    # soft limit: exceeding the grid limit is allowed but very costly
    c[ix["MDo"]] = 0.01     # tiny tie-break: keep unbilled-hour imports flat, not spiky
    c[ix["CapO"]] = 1000.0  # soft cap on total grid energy: peak savings are worth ~RM 8 per kWh here, so this must be far higher

    bounds = [(0, None)] * nv
    for t in range(N):
        bounds[ix["Ch"][t]] = (0, P)
        bounds[ix["Dis"][t]] = (0, P)
        bounds[ix["Flex"][t]] = (0, cp if t in win else 0)
        bounds[ix["PVu"][t]] = (0, pv[t])
    for t in range(N + 1):
        bounds[ix["SOC"][t]] = (0, cap)
    bounds[ix["SOC"][0]] = (0.5 * cap, 0.5 * cap)      # start at 50%
    bounds[ix["SOC"][N]] = (0.5 * cap, cap)            # end at least at start

    Aeq, beq = [], []
    for t in range(N):
        r = np.zeros(nv)
        r[ix["Imp"][t]] = 1; r[ix["Dis"][t]] = 1; r[ix["Ch"][t]] = -1
        r[ix["Flex"][t]] = -1; r[ix["PVu"][t]] = 1
        Aeq.append(r); beq.append(fixed[t])
        s = np.zeros(nv)
        s[ix["SOC"][t + 1]] = 1; s[ix["SOC"][t]] = -1
        s[ix["Ch"][t]] = -eta; s[ix["Dis"][t]] = 1 / eta
        Aeq.append(s); beq.append(0.0)
    r = np.zeros(nv); r[ix["Flex"][win]] = 1
    Aeq.append(r); beq.append(flex_total)

    Aub, bub = [], []
    for t in md_hours:
        r = np.zeros(nv); r[ix["Imp"][t]] = 1; r[ix["MD"][0]] = -1
        Aub.append(r); bub.append(0.0)

    for t in range(N):
        if limit > 0:
            r = np.zeros(nv); r[ix["Imp"][t]] = 1; r[ix["Over"][t]] = -1
            Aub.append(r); bub.append(limit)
        if t not in md_hours:
            r = np.zeros(nv); r[ix["Imp"][t]] = 1; r[ix["MDo"][0]] = -1
            Aub.append(r); bub.append(0.0)

    if gcap > 0:
        r = np.zeros(nv); r[ix["Imp"]] = 1; r[ix["CapO"][0]] = -1
        Aub.append(r); bub.append(gcap)

    res = linprog(c, A_ub=np.array(Aub) if Aub else None, b_ub=bub if Aub else None,
                  A_eq=np.array(Aeq), b_eq=beq, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError("optimisation failed: " + res.message)
    x = res.x
    g = lambda n_: [round(float(v), 3) for v in x[ix[n_]]]
    grid = x[ix["Imp"]]
    peak = float(max(grid[h] for h in md_hours)) if md_hours else float(grid.max())
    over = float(x[ix["Over"]].max())
    bill = float(rate @ grid + md_rate * peak)      # true bill, without carbon or wear weights
    return {"cap_over_kwh": float(x[ix["CapO"][0]]), "max_grid": float(grid.max()), "over_kw": over, "cost": bill, "peak": peak, "grid": g("Imp"), "dis": g("Dis"),
            "ch": g("Ch"), "flex": g("Flex"), "pv_used": g("PVu"),
            "ld": [round(float(v), 3) for v in fixed + x[ix["Flex"]]]}
