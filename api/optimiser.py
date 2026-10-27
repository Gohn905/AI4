"""Cost optimiser (hourly, or half-hourly with 48 values per profile) for a facility with solar, battery and shiftable load.

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

N = 24                    # default: hourly. Send 48 values per profile for half-hourly steps (billed maximum demand is a 30-minute figure).


def _steps(d):
    """Number of time steps: 24 (hourly) or 48 (half-hourly), taken from the length of 'load'."""
    v = d.get("load")
    n = len(v) if isinstance(v, list) else 0
    if n not in (24, 48):
        raise ValueError("load must be a list of 24 (hourly) or 48 (half-hourly) numbers")
    return n


def _vec(d, key, lo, hi, n=N):
    v = d.get(key)
    if not isinstance(v, list) or len(v) != n:
        raise ValueError(f"{key} must be a list of {n} numbers, the same length as load")
    a = np.array(v, dtype=float)
    if not np.all(np.isfinite(a)) or a.min() < lo or a.max() > hi:
        raise ValueError(f"{key} out of range")
    return a


def _num(d, key, lo, hi):
    x = float(d.get(key, 0))
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return x


def operating_window(weekday):
    """Charging window (start_hour, end_hour) for the EV chargers by weekday (Mon=0 ... Sun=6), or None when closed.
    Mon-Fri 07:30-18:00, Sat 07:30-14:00, Sun closed (organiser brief)."""
    if weekday <= 4:
        return (7.5, 18.0)
    if weekday == 5:
        return (7.5, 14.0)
    return None


def _ev(d, N, dt):
    """Parse the optional EV charger block:
       {"chargers": 8, "kw": 50, "energy_kwh": 300, "window": [7.5, 18.0]}   (window null or [] = no charging today)
    Returns None when absent, else the steps inside the window, the power cap and the energy that can be delivered."""
    e = d.get("ev")
    if not e:
        return None
    if not isinstance(e, dict):
        raise ValueError("ev must be an object")
    try:
        chargers, kw, energy = float(e.get("chargers", 8)), float(e.get("kw", 50)), float(e.get("energy_kwh", 0))
    except (TypeError, ValueError):
        raise ValueError("ev numbers must be numeric")
    if not all(math.isfinite(v) for v in (chargers, kw, energy)) or not 0 <= chargers <= 1000 or not 0 <= kw <= 1000 or not 0 <= energy <= 1e6:
        raise ValueError("ev values out of range")
    w = e.get("window")
    steps, window = [], None
    if w:
        try:
            a, b = float(w[0]), float(w[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError("ev window must be [start_hour, end_hour]")
        if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b <= 24):
            raise ValueError("ev window must satisfy 0 <= start < end <= 24")
        window = [a, b]
        steps = [t for t in range(N) if t * dt >= a - 1e-9 and (t + 1) * dt <= b + 1e-9]
    kw_max = chargers * kw
    used = min(energy, kw_max * dt * len(steps))
    return {"kw_max": kw_max, "steps": steps, "window": window, "energy": energy, "used": used, "unmet": energy - used}


def _fixed(d, N):
    """Optional {"ch","dis","flex"}: kW lists for the first k steps that have already happened (battery charge, battery
    discharge, EV charging). The optimiser then only decides steps k..N-1 (rolling-horizon re-planning). Grid import and solar
    use for those steps are derived from the energy balance with the load and solar given for them, so the history can never
    be inconsistent."""
    f = d.get("fixed")
    if not f:
        return None
    if not isinstance(f, dict):
        raise ValueError("fixed must be an object")
    out, k = {}, None
    for key in ("ch", "dis", "flex"):
        v = f.get(key)
        if not isinstance(v, list) or not 0 < len(v) < N:
            raise ValueError(f"fixed.{key} must be a list with 1 to {N - 1} numbers")
        if k is None:
            k = len(v)
        elif len(v) != k:
            raise ValueError("fixed lists must all have the same length")
        a = np.array(v, dtype=float)
        if not np.all(np.isfinite(a)) or a.min() < -1e-6 or a.max() > 1e7:
            raise ValueError(f"fixed.{key} out of range")
        out[key] = np.maximum(a, 0.0)
    out["k"] = k
    return out


def ev_baseline(d):
    """What the chargers would draw with no smart scheduling: full power from the start of the window until the energy is delivered."""
    N = _steps(d)
    dt = 24.0 / N
    ev = _ev(d, N, dt)
    out = [0.0] * N
    if ev:
        left = ev["used"]
        for t in ev["steps"]:
            p = min(ev["kw_max"], left / dt)
            out[t] = round(p, 3)
            left -= p * dt
    return out


def optimise(d):
    """Plan the day. Optional "soc_reserve" (0 to 0.5): share of the battery kept in reserve for the whole day (outages, forecast error).
    The reserve is a comfort rule, the grid limit is a safety rule: if keeping the reserve makes a hard grid limit impossible, the
    plan is made again without the reserve and the result says so ("reserve_relaxed": true)."""
    rsv = d.get("soc_reserve", 0) or 0
    if rsv and not d.get("_rsv_pass"):
        r = optimise({**d, "_rsv_pass": True})
        limited = float(d.get("grid_limit", 0) or 0) > 0 and bool(d.get("hard_limit", True))
        if limited and not r.get("limit_ok", True):
            r0 = optimise({**d, "_rsv_pass": True, "soc_reserve": 0})
            r0["reserve_relaxed"] = True
            return r0
        r["reserve_relaxed"] = False
        return r
    N = _steps(d)                            # 24 or 48 steps
    dt = 24.0 / N                            # hours per step: kW x dt = kWh
    load = _vec(d, "load", 0, 1e6, N)        # kW, original load (average over each step)
    pv = _vec(d, "pv", 0, 1e6, N)            # kW, available solar
    rate = _vec(d, "rates", 0, 10, N)        # RM/kWh incl. adders and AFA
    md_rate = _num(d, "md_rate_day", 0, 1000)  # RM/kW/day
    cap = _num(d, "batt_kwh", 0, 20000)
    share = _num(d, "flex_share", 0, 0.5)
    carbon = _num(d, "carbon_adder", 0, 10)   # RM/kWh of grid energy (carbon price x emission factor)
    limit = _num(d, "grid_limit", 0, 1e6)    # kW cap on grid import in ANY hour, 0 = none
    hard = bool(d.get("hard_limit", True)) and limit > 0   # grid limit is a hard rule when it can be met
    wear = _num(d, "wear", 0, 10)             # RM per kWh of battery throughput
    eff = float(d.get("eff", 0.90))
    if not math.isfinite(eff) or not 0.5 <= eff <= 1.0:
        raise ValueError("eff must be between 0.5 and 1.0")
    eta = math.sqrt(eff)
    reserve = _num(d, "soc_reserve", 0, 0.5)   # share of the battery never used (kept for outages and forecast error)
    allow = d.get("co2_allow")
    if allow is not None:
        allow = float(allow)
        if not math.isfinite(allow) or not 0 <= allow <= 1:
            raise ValueError("co2_allow must be between 0 and 1")
    gcap = float(d.get("grid_kwh_cap", 0) or 0)
    if not math.isfinite(gcap) or gcap < 0:
        raise ValueError("grid_kwh_cap must be zero or more")
    fx = _fixed(d, N)                         # optional: steps already executed (re-optimisation), locked to what really happened
    soc_now = d.get("soc_now_kwh")            # optional: measured battery charge at the end of the fixed steps
    if soc_now is not None:
        if not fx:
            raise ValueError("soc_now_kwh needs fixed steps")
        try:
            soc_now = float(soc_now)
        except (TypeError, ValueError):
            raise ValueError("soc_now_kwh must be a number")
        if not math.isfinite(soc_now) or not 0 <= soc_now <= cap + 1e-6:
            raise ValueError("soc_now_kwh must be between 0 and the battery size")
    if allow is not None and allow < 1 and gcap == 0:
        # The allowance is always measured against the cheapest plan for the WHOLE day. A re-plan locks the hours already gone
        # ("fixed") but must keep the same allowance: measuring it against what is left of the day halves it a second time, and the
        # re-plan then costs more than just carrying on with the old plan even when nothing in the forecast was wrong.
        free = optimise({**{k: v for k, v in d.items() if k not in ("fixed", "soc_now_kwh")}, "co2_allow": 1})   # the cheapest plan, no cap
        base = float(np.maximum(load + np.array(ev_baseline(d)) - pv, 0).sum()) * dt   # solar-only baseline grid energy incl. EVs charging immediately, kWh
        extra = float(sum(free["grid"])) * dt - base
        if extra <= 0.5:                                        # nothing extra to hold back
            return free if "fixed" not in d else optimise({**d, "co2_allow": 1})
        return optimise({**d, "co2_allow": 1, "grid_kwh_cap": base + allow * extra})
    per = N // 24                             # steps per hour
    md_hours = sorted({h * per + j for h in (int(x) for x in d.get("md_hours", []) if 0 <= int(x) < 24) for j in range(per)})
    P = cap / 2.0
    ev = _ev(d, N, dt)
    fixed = load.copy()
    if ev:                                    # EV chargers: only these are flexible, only inside the operating window
        win, flex_total, cp = ev["steps"], ev["used"], ev["kw_max"]
    else:                                     # older behaviour: a share of the 09:00-17:00 load can move
        win = list(range(9 * per, 17 * per))
        flex_total = share * load[win].sum() * dt            # kWh
        fixed[win] = load[win] * (1 - share)
        cp = 1.5 * flex_total / (len(win) * dt)         # kW

    ix, k = {}, 0
    for name, size in [("Imp", N), ("MD", 1), ("Ch", N), ("Dis", N),
                       ("SOC", N + 1), ("Flex", N), ("PVu", N), ("Over", N), ("MDo", 1), ("CapO", 1), ("Dev", N)]:
        ix[name] = np.arange(k, k + size); k += size
    nv = k
    c = np.zeros(nv); c[ix["Imp"]] = (rate + carbon) * dt; c[ix["MD"]] = md_rate
    c[ix["Ch"]] = wear * dt; c[ix["Dis"]] = wear * dt
    c[ix["Over"]] = 50.0 * dt    # soft limit: exceeding the grid limit is allowed but very costly
    c[ix["MDo"]] = 0.01 * dt     # tiny tie-break: keep unbilled-hour imports flat, not spiky
    # Tie-break: when moving EV charging does not lower the bill, leave it where it would normally be (charge immediately).
    # 0.005 RM per kWh moved is far below any real saving (smallest tariff gap is 4 sen/kWh), so a shift that pays is never blocked.
    # Send "shift_penalty": 0 to switch this off.
    shift_pen = float(d.get("shift_penalty", 0.005))
    if not math.isfinite(shift_pen) or not 0 <= shift_pen <= 0.02:
        raise ValueError("shift_penalty must be between 0 and 0.02")
    c[ix["Dev"]] = shift_pen * dt
    c[ix["CapO"]] = 1000.0  # soft cap on total grid energy: peak savings are worth ~RM 8 per kWh here, so this must be far higher

    bounds = [(0, None)] * nv
    for t in range(N):
        bounds[ix["Ch"][t]] = (0, P)
        bounds[ix["Dis"][t]] = (0, P)
        bounds[ix["Flex"][t]] = (0, cp if t in win else 0)
        bounds[ix["PVu"][t]] = (0, pv[t])
        if hard:
            bounds[ix["Imp"][t]] = (0, limit)
    for t in range(N + 1):
        bounds[ix["SOC"][t]] = (0, cap)
    bounds[ix["SOC"][0]] = (0.5 * cap, 0.5 * cap)      # start at 50%
    bounds[ix["SOC"][N]] = (0.5 * cap, cap)            # end at least at start
    if reserve > 0 and cap > 0:                         # never drop below the reserve during the day
        floor = reserve * cap
        if soc_now is not None:
            floor = min(floor, float(soc_now))          # a re-plan cannot demand more than the meter says is there
        for t in range(1, N):
            bounds[ix["SOC"][t]] = (floor, cap)
    if fx:                                    # past steps are history: executed decisions, not decisions to make
        for t in range(fx["k"]):
            ch_, dis_, fl_ = float(fx["ch"][t]), float(fx["dis"][t]), float(fx["flex"][t])
            need = fixed[t] + fl_ + ch_ - dis_
            if need < 0:                      # discharging more than the site can use is not allowed (no export)
                dis_ += need
                need = 0.0
            used = min(float(pv[t]), need)
            for name, v in (("Imp", need - used), ("Ch", ch_), ("Dis", dis_), ("Flex", fl_), ("PVu", used)):
                bounds[ix[name][t]] = (v, v)
        for t in range(1, fx["k"] + 1):       # 0.001 kWh of slack so rounded history cannot make the problem infeasible
            bounds[ix["SOC"][t]] = (-1e-3, cap + 1e-3)
        if soc_now is not None:               # trust the meter: the plan continues from the measured charge
            for t in range(fx["k"]):
                bounds[ix["SOC"][t]] = (-1e7, 1e7)       # history before the measurement is only bookkeeping
            bounds[ix["SOC"][fx["k"]]] = (soc_now, soc_now)

    Aeq, beq = [], []
    for t in range(N):
        r = np.zeros(nv)
        r[ix["Imp"][t]] = 1; r[ix["Dis"][t]] = 1; r[ix["Ch"][t]] = -1
        r[ix["Flex"][t]] = -1; r[ix["PVu"][t]] = 1
        Aeq.append(r); beq.append(fixed[t])
        s = np.zeros(nv)
        s[ix["SOC"][t + 1]] = 1; s[ix["SOC"][t]] = -1
        s[ix["Ch"][t]] = -eta * dt; s[ix["Dis"][t]] = dt / eta
        Aeq.append(s); beq.append(0.0)
    r = np.zeros(nv); r[ix["Flex"][win]] = dt
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

    if ev and shift_pen > 0:                  # Dev[t] >= |Flex[t] - baseline[t]|
        base_ev = np.array(ev_baseline(d))
        for t in range(N):
            r = np.zeros(nv); r[ix["Flex"][t]] = 1; r[ix["Dev"][t]] = -1; Aub.append(r); bub.append(base_ev[t])
            r = np.zeros(nv); r[ix["Flex"][t]] = -1; r[ix["Dev"][t]] = -1; Aub.append(r); bub.append(-base_ev[t])
    if gcap > 0:
        r = np.zeros(nv); r[ix["Imp"]] = dt; r[ix["CapO"][0]] = -1
        Aub.append(r); bub.append(gcap)

    res = linprog(c, A_ub=np.array(Aub) if Aub else None, b_ub=bub if Aub else None,
                  A_eq=np.array(Aeq), b_eq=beq, bounds=bounds, method="highs")
    if not res.success and hard:              # the limit cannot be met at all: plan as close to it as possible and say so
        return optimise({**d, "hard_limit": False})
    if not res.success:
        raise RuntimeError("optimisation failed: " + res.message)
    x = res.x
    g = lambda n_: [round(float(v), 3) for v in x[ix[n_]]]
    grid = x[ix["Imp"]]
    peak = float(max(grid[h] for h in md_hours)) if md_hours else float(grid.max())
    over = float(x[ix["Over"]].max())
    bill = float(rate @ grid * dt + md_rate * peak)      # true bill, without carbon or wear weights
    extra = {"limit_ok": bool(limit <= 0 or float(grid.max()) <= limit + 1e-6)}
    if ev:
        extra.update(ev_window=ev["window"], ev_unmet_kwh=round(ev["unmet"], 3), ev_baseline=ev_baseline(d))
    return {**extra, "steps": N, "cap_over_kwh": float(x[ix["CapO"][0]]), "max_grid": float(grid.max()), "over_kw": over, "cost": bill, "peak": peak, "grid": g("Imp"), "dis": g("Dis"),
            "ch": g("Ch"), "flex": g("Flex"), "pv_used": g("PVu"),
            "ld": [round(float(v), 3) for v in fixed + x[ix["Flex"]]]}
