"""Reference strategies and KPIs, so the optimiser is compared fairly.

Three strategies are evaluated on EXACTLY the same inputs (same load, solar, EV energy, rates, demand-charge hours, limit):

  solar_only    Solar covers load first, the rest comes from the grid. No battery. EVs charge as soon as they arrive.
  battery_rule  The same, plus a battery run by a simple rule (self-consumption): charge from surplus solar only, discharge
                whenever the load is above solar. Never charges from the grid, never looks at tariffs or the limit.
                This is what a battery does without any optimisation, so it separates the value of the battery from the
                value of the optimiser.
  optimised     The linear program in optimiser.py.

All three use the same KPI function (kpis), so a number means the same thing for each. Nothing here is hard-coded: every
figure is calculated from the time series.
Units: kW per step, kWh = kW x dt, RM. No export income is modelled: solar that cannot be used or stored is curtailed.
"""
import math
import numpy as np
from optimiser import _steps, _vec, _num, ev_baseline, optimise


def _inputs(d):
    n = _steps(d)
    dt = 24.0 / n
    load = _vec(d, "load", 0, 1e6, n)
    pv = _vec(d, "pv", 0, 1e6, n)
    rate = _vec(d, "rates", 0, 10, n)
    md_rate = _num(d, "md_rate_day", 0, 1000)
    cap = _num(d, "batt_kwh", 0, 20000)
    limit = _num(d, "grid_limit", 0, 1e6)
    eff = float(d.get("eff", 0.90))
    if not math.isfinite(eff) or not 0.5 <= eff <= 1.0:
        raise ValueError("eff must be between 0.5 and 1.0")
    per = n // 24
    md_hours = sorted({h * per + j for h in (int(x) for x in d.get("md_hours", []) if 0 <= int(x) < 24) for j in range(per)})
    return n, dt, load, pv, rate, md_rate, cap, limit, eff, md_hours


def kpis(d, grid, pv_used, ch, dis, total_load, ev_unmet=0.0):
    """KPIs from time series (all kW lists of the same length). total_load is the load the site really had to serve in
    each step, including EV charging, but not battery charging."""
    n, dt, load, pv, rate, md_rate, cap, limit, eff, md_hours = _inputs(d)
    g, u, c, x, tl = (np.asarray(a, dtype=float) for a in (grid, pv_used, ch, dis, total_load))
    if not all(len(a) == n for a in (g, u, c, x, tl)):
        raise ValueError("series length does not match the number of steps")
    peak = float(max(g[h] for h in md_hours)) if md_hours else float(g.max())
    energy_cost = float(rate @ g) * dt
    demand_cost = md_rate * peak
    avail = float(pv.sum()) * dt
    used = float(u.sum()) * dt
    over = np.maximum(g - limit, 0) if limit > 0 else np.zeros(n)
    over = np.where(over > 0.01, over, 0.0)           # ignore rounding noise below 0.01 kW
    return {
        "cost": energy_cost + demand_cost,
        "energy_cost": energy_cost,
        "demand_cost": demand_cost,
        "grid_kwh": float(g.sum()) * dt,
        "billed_peak_kw": peak,
        "max_grid_kw": float(g.max()),
        "solar_available_kwh": avail,
        "solar_used_kwh": used,
        "solar_curtailed_kwh": max(avail - used, 0.0),
        "solar_self_consumption_pct": 100.0 * used / avail if avail > 0 else None,
        "limit_kw": limit,
        "limit_violation_steps": int((over > 0).sum()),
        "limit_max_excess_kw": float(over.max()),
        "battery_throughput_kwh": float((c.sum() + x.sum())) * dt,
        "ev_unmet_kwh": float(ev_unmet),
        "balance_error_kw": float(np.abs(g + x + u - tl - c).max()),   # should be ~0: grid + discharge + solar used = load + charging
    }


def solar_only(d):
    n, dt, load, pv, *_ = _inputs(d)
    tl = load + np.array(ev_baseline(d))
    u = np.minimum(tl, pv)
    g = tl - u
    z = np.zeros(n)
    k = kpis(d, g, u, z, z, tl, _ev_unmet(d))
    k.update(grid=list(g), pv_used=list(u), ch=list(z), dis=list(z), load=list(tl))
    return k


def battery_rule(d, mode="self"):
    """Battery without optimisation. Starts at 50% like the optimiser.
      mode "self": self-consumption. Charge only from surplus solar, discharge whenever load is above solar. No tariff or limit awareness.
      mode "tou":  simple time-of-use scheduler, a common commercial default. In the cheapest-rate steps charge at full power
                   (surplus solar first, then grid, never pushing grid import above the limit); in every dearer step discharge
                   at full power against the load. It does not look at the demand charge or forecast, so it can run flat early.
    If it ends the day below 50%, the missing energy is bought back at the day's cheapest rate (soc_adjust_cost), because the
    optimiser must also end at least at 50%. Ending above 50% earns no credit, for either strategy."""
    n, dt, load, pv, rate, md_rate, cap, limit, eff, md_hours = _inputs(d)
    eta = math.sqrt(eff)
    P = cap / 2.0
    tl = load + np.array(ev_baseline(d))
    soc = soc0 = 0.5 * cap
    cheap = rate <= rate.min() + 1e-9
    g, u, c, x = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n)
    for t in range(n):
        net = tl[t] - pv[t]                                   # >0: deficit, <0: surplus solar
        if mode == "tou" and not cheap.all() and not cheap[t]:
            x[t] = min(max(net, 0.0), P, soc * eta / dt) if cap > 0 else 0.0     # discharge, never export
        elif net < 0:
            c[t] = min(-net, P, (cap - soc) / (eta * dt)) if cap > 0 else 0.0     # surplus solar into the battery
        elif mode == "self":
            x[t] = min(net, P, soc * eta / dt) if cap > 0 else 0.0
        elif cap > 0 and cheap[t] and not cheap.all():                           # tou: charge from the grid when cheap
            room = (limit - net) if limit > 0 else P
            c[t] = max(0.0, min(P, room, (cap - soc) / (eta * dt)))
        u[t] = min(pv[t], tl[t] + c[t])
        g[t] = tl[t] + c[t] - x[t] - u[t]
        soc += eta * dt * c[t] - dt / eta * x[t]
    short = max(soc0 - soc, 0.0)
    adjust = short / eta * float(rate.min())
    k = kpis(d, g, u, c, x, tl, _ev_unmet(d))
    k.update(soc_end_kwh=soc, soc_shortfall_kwh=short, soc_adjust_cost=adjust)
    k["cost"] += adjust
    k.update(grid=list(g), pv_used=list(u), ch=list(c), dis=list(x), load=list(tl))
    return k


def optimised(d):
    o = optimise(d)
    n = o["steps"]
    tl = np.array(o["ld"], dtype=float)                      # load actually served, including moved EV charging
    k = kpis(d, o["grid"], o["pv_used"], o["ch"], o["dis"], tl, o.get("ev_unmet_kwh", 0.0))
    k.update(grid=o["grid"], pv_used=o["pv_used"], ch=o["ch"], dis=o["dis"], load=list(tl), flex=o["flex"], limit_ok=o["limit_ok"])
    return k


def _ev_unmet(d):
    e = d.get("ev")
    if not e:
        return 0.0
    n = _steps(d)
    from optimiser import _ev
    ev = _ev(d, n, 24.0 / n)
    return ev["unmet"] if ev else 0.0


def pct(a, b):
    """Percentage cost reduction of b versus a."""
    return 100.0 * (a - b) / a if a > 0 else None


def compare(d, series=True):
    """Same inputs, four strategies. Returns KPIs for each plus the savings split into battery value and optimiser value."""
    so, bs, bt, op = solar_only(d), battery_rule(d, "self"), battery_rule(d, "tou"), optimised(d)
    out = {
        "solar_only": so, "battery_self": bs, "battery_tou": bt, "optimised": op,
        "savings": {
            "optimised_vs_solar_only_pct": pct(so["cost"], op["cost"]),
            "optimised_vs_battery_self_pct": pct(bs["cost"], op["cost"]),
            "optimised_vs_battery_tou_pct": pct(bt["cost"], op["cost"]),
            "optimised_vs_solar_only_rm": so["cost"] - op["cost"],
            "optimised_vs_battery_self_rm": bs["cost"] - op["cost"],
            "optimised_vs_battery_tou_rm": bt["cost"] - op["cost"],
        },
        "definitions": {
            "solar_only": "Solar first, rest from grid. No battery. EVs charge immediately.",
            "battery_self": "As solar_only plus a battery that charges only from surplus solar and discharges whenever load exceeds solar. No tariff or limit awareness.",
            "battery_tou": "As solar_only plus a simple time-of-use battery: charge in the cheapest-rate steps (within the grid limit), discharge at full power in dearer steps. Ignores the demand charge and the forecast.",
            "optimised": "HiGHS linear program: battery and EV charging scheduled for the lowest bill, grid limit enforced when it can be met.",
            "same_inputs": "Load, solar, EV energy, rates, demand-charge hours, limit and battery size are identical for all four.",
        },
    }
    refs = {"solar_only": so, "battery_self": bs, "battery_tou": bt}
    best = min(refs, key=lambda k: refs[k]["cost"])
    out["savings"].update(best_reference=best, best_reference_cost=refs[best]["cost"],
                          optimised_vs_best_reference_pct=pct(refs[best]["cost"], op["cost"]),
                          optimised_vs_best_reference_rm=refs[best]["cost"] - op["cost"])
    if not series:
        for k in ("solar_only", "battery_self", "battery_tou", "optimised"):
            for key in ("grid", "pv_used", "ch", "dis", "load", "flex"):
                out[k].pop(key, None)
    return out
