"""
Microgrid Energy Management System (MEMS) - MILP in Python.

Port of the MATLAB intlinprog model from the APU GTD assignment, using
scipy.optimize.milp. Extra, optional modules for the hackathon brief:
  - battery storage (energy, power, round-trip efficiency, cyclic SOC)
  - flexible load (a share of daytime load that can move within a window)

Run:  python mems_milp.py
"""
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds

# ---- Case-study data (APU weekday, from the assignment) -------------------
LOAD = np.array([350, 345, 345, 345, 345, 345, 380, 450, 700, 850, 880, 900,
                 780, 970, 1050, 1010, 880, 750, 600, 500, 420, 380, 360, 355], float)
PV = np.array([0, 0, 0, 0, 0, 0, 0, 0, 20, 50, 80, 95,
               100, 90, 80, 60, 40, 10, 0, 0, 0, 0, 0, 0], float)
N = 24

# ---- Tariff and generator parameters (TNB medium-voltage ToU, 2026 sheet) --
PEAK_TARIFF = 0.3132       # RM/kWh, weekday peak window (see PEAK_START / PEAK_END)
OFFPEAK_TARIFF = 0.2723    # RM/kWh
MD_RATE = 97.06            # RM/kW/month (capacity 30.19 + network 66.87)
DIESEL_PRICE = 4.57        # RM/L
DG_FUEL_LPH, DG_KW = 83.0, 300.0
DG_MIN, DG_MAX = 50.0, 300.0
GRID_MAX = 5000.0
DG_COST = DIESEL_PRICE * DG_FUEL_LPH / DG_KW   # RM/kWh (about 1.264)
# TNB RP4 (from 1 Jul 2025): ToU peak is 14:00-22:00 on weekdays, and on MV/HV ToU
# only demand inside the peak window is charged. The 2006 tariff booklet used 08:00-22:00.
PEAK_START, PEAK_END = 14, 22


def solve(load=LOAD, pv=PV, use_dg=True, peak_start=PEAK_START, peak_end=PEAK_END, md_peak_only=True,
          batt_kwh=0.0, batt_kw=0.0, eff_round_trip=0.90,
          flex_share=0.0, flex_window=(9, 17), flex_hour_cap=1.5):
    """Return dict of results. All profiles are hourly (kW == kWh per hour)."""
    n = N
    tariff = np.array([PEAK_TARIFF if peak_start <= h < peak_end else OFFPEAK_TARIFF for h in range(n)])
    md_hours = [h for h in range(n) if (peak_start <= h < peak_end) or not md_peak_only]
    eta = np.sqrt(eff_round_trip)

    # Flexible load: share of load inside the window becomes a decision variable.
    win = np.arange(flex_window[0], flex_window[1])
    fixed = load.copy()
    flex_total = 0.0
    if flex_share > 0:
        flex_total = flex_share * load[win].sum()
        fixed[win] = load[win] * (1 - flex_share)

    # Variable layout: DG, Imp, Exp, DGon, ImpOn, MD, Ch, Dis, SOC(n+1), Flex
    idx, k = {}, 0
    for name, size in [("DG", n), ("Imp", n), ("Exp", n), ("DGon", n), ("ImpOn", n),
                       ("MD", 1), ("Ch", n), ("Dis", n), ("SOC", n + 1), ("Flex", n)]:
        idx[name] = np.arange(k, k + size)
        k += size
    nv = k

    c = np.zeros(nv)
    c[idx["DG"]] = DG_COST
    c[idx["Imp"]] = tariff
    c[idx["MD"]] = MD_RATE / 30.0           # monthly MD charge spread over 30 days

    lb, ub = np.zeros(nv), np.zeros(nv)
    ub[idx["DG"]] = DG_MAX if use_dg else 0
    ub[idx["Imp"]] = GRID_MAX
    ub[idx["Exp"]] = 0.0                    # NEM guideline: no export in this case study
    ub[idx["DGon"]] = 1 if use_dg else 0
    ub[idx["ImpOn"]] = 1
    ub[idx["MD"]] = GRID_MAX
    cap = batt_kwh
    ub[idx["Ch"]] = batt_kw
    ub[idx["Dis"]] = batt_kw
    ub[idx["SOC"]] = cap
    if flex_share > 0:
        per_hour = flex_hour_cap * flex_total / len(win)
        ub[idx["Flex"][win]] = per_hour

    integrality = np.zeros(nv)
    integrality[idx["DGon"]] = 1
    integrality[idx["ImpOn"]] = 1

    rows, lo, hi = [], [], []
    def add(coefs, low, high):
        r = np.zeros(nv)
        for j, v in coefs:
            r[j] += v
        rows.append(r); lo.append(low); hi.append(high)

    for t in range(n):
        # power balance: PV + DG + Imp + Dis - Exp - Ch - Flex_t = fixed load
        coefs = [(idx["DG"][t], 1), (idx["Imp"][t], 1), (idx["Exp"][t], -1),
                 (idx["Dis"][t], 1), (idx["Ch"][t], -1), (idx["Flex"][t], -1)]
        add(coefs, fixed[t] - pv[t], fixed[t] - pv[t])
        # DG on/off limits
        add([(idx["DG"][t], 1), (idx["DGon"][t], -DG_MAX)], -np.inf, 0)
        add([(idx["DG"][t], -1), (idx["DGon"][t], DG_MIN)], -np.inf, 0)
        # grid on/off and maximum demand
        add([(idx["Imp"][t], 1), (idx["ImpOn"][t], -GRID_MAX)], -np.inf, 0)
        if t in md_hours:
            add([(idx["Imp"][t], 1), (idx["MD"][0], -1)], -np.inf, 0)
        # battery state of charge
        add([(idx["SOC"][t + 1], 1), (idx["SOC"][t], -1),
             (idx["Ch"][t], -eta), (idx["Dis"][t], 1 / eta)], 0, 0)
    # cyclic battery: end the day at the starting charge (start at 50 percent)
    add([(idx["SOC"][0], 1)], 0.5 * cap, 0.5 * cap)
    add([(idx["SOC"][n], 1)], 0.5 * cap, np.inf)
    # flexible energy is conserved
    if flex_share > 0:
        add([(j, 1) for j in idx["Flex"][win]], flex_total, flex_total)
    # no flexible load outside the window
    for t in range(n):
        if t not in win:
            ub[idx["Flex"][t]] = 0

    res = milp(c, constraints=LinearConstraint(np.array(rows), lo, hi),
               integrality=integrality, bounds=Bounds(lb, ub))
    if not res.success:
        raise RuntimeError("MILP failed: " + res.message)
    x = res.x
    out = {k_: x[v] for k_, v in idx.items()}
    imp, dg = out["Imp"], out["DG"]
    out.update(
        total_cost=res.fun,
        dg_cost=float(DG_COST * dg.sum()),
        grid_cost=float((tariff * imp).sum()),
        md_cost=float(MD_RATE * out["MD"][0] / 30.0),
        max_grid_kw=float(out["MD"][0]),
        dg_hours=int(round(out["DGon"].sum())),
        grid_kwh=float(imp.sum()),
        dg_kwh=float(dg.sum()),
    )
    # power balance check against the original (unshifted) load energy
    served = pv + dg + imp + out["Dis"] - out["Ch"] - out["Flex"]
    out["balance_error"] = float(np.abs(served - fixed).max())
    return out


def show(title, r):
    print(f"\n{title}")
    print(f"  Total net cost : RM {r['total_cost']:.2f}")
    print(f"  Grid energy    : RM {r['grid_cost']:.2f}   Max demand: RM {r['md_cost']:.2f}"
          f"   DG: RM {r['dg_cost']:.2f}")
    print(f"  Max grid demand: {r['max_grid_kw']:.0f} kW   DG hours: {r['dg_hours']}"
          f"   balance error: {r['balance_error']:.1e}")


if __name__ == "__main__":
    # A. Reproduce the assignment exactly (2006 booklet window, MD on all hours)
    old = dict(peak_start=8, peak_end=22, md_peak_only=False)
    show("A1. Assignment, no diesel (expect RM 7270.09)", solve(use_dg=False, **old))
    show("A2. Assignment, with diesel (expect RM 7125.34)", solve(use_dg=True, **old))
    # B. Current RP4 ToU window: peak 14:00-22:00, MD on peak hours only
    base = solve(use_dg=False)
    show("B1. RP4 window, no diesel (baseline)", base)
    asg = solve(use_dg=True)
    show("B2. RP4 window, with diesel", asg)
    b = solve(use_dg=False, batt_kwh=500, batt_kw=250)
    show("B3. RP4 window, battery 500 kWh / 250 kW", b)
    bf = solve(use_dg=False, batt_kwh=500, batt_kw=250, flex_share=0.10)
    show("B4. RP4 window, battery + 10% shiftable load", bf)
    for name, r in [("Diesel", asg), ("Battery", b), ("Battery + flex", bf)]:
        print(f"Saving vs baseline, {name:15s}: "
              f"{(1 - r['total_cost'] / base['total_cost']) * 100:.1f}%")
