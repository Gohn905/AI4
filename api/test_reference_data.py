"""Shared synthetic test day for test_reference.py and test_reopt.py (SYNTHETIC data, not measurements)."""
import math

H = [t / 2 for t in range(48)]
def make(peak=4000, kwp=3000, cloud=1.0, bat=5000, lim=4200, ev=True, wk=True):
    load = [peak * (0.55 + 0.35 * math.exp(-((h - 13) / 5) ** 2) + 0.1 * math.exp(-((h - 20) / 2) ** 2)) * (1 if wk else .6) for h in H]
    pv = [max(0.0, kwp * .85 * math.sin(math.pi * (h - 6.5) / 12)) * cloud if 6.5 < h < 18.5 else 0.0 for h in H]
    pk = lambda h: wk and 14 <= h < 22
    d = {"load": load, "pv": pv, "rates": [0.3132 if pk(h) else 0.2723 for h in H], "md_rate_day": 97.06 / 30 if wk else 0,
         "md_hours": list(range(14, 22)) if wk else [], "batt_kwh": bat, "flex_share": 0, "carbon_adder": 0, "grid_limit": lim,
         "hard_limit": True, "wear": 0, "eff": 0.9, "co2_allow": 1}
    if ev:
        d["ev"] = {"chargers": 8, "kw": 50, "energy_kwh": 700, "window": [7.5, 18.0] if wk else None}
    return d

