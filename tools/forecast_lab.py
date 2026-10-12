#!/usr/bin/env python3
"""Train and test the solar and demand forecasts, then write api/model.json.

Run from the repo:      python tools/forecast_lab.py
Self-test, no internet: python tools/forecast_lab.py --offline
Real meter data:        python tools/forecast_lab.py --meter meter.csv   (columns: timestamp,kw)

Needs only numpy. Put forecast.py (from api/) in the same folder or keep the repo layout.
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "..", "api")):
    sys.path.insert(0, p)
import forecast as fc  # noqa: E402

NOTE_SYNTH = ("Demand history is SYNTHETIC (shape from the case study, plus weekend, cooling and noise "
              "assumptions). It shows the pipeline works, not real-world accuracy.")
NOTE_METER = "Demand model trained on the user's own meter data."


# ---------- offline mock weather (self-test only) ----------
def mock_weather(start, end, seed=1):
    rng = np.random.default_rng(seed)
    F, A = {}, {}
    d = start
    while d <= end:
        c = float(rng.beta(2, 3))                          # actual cloudiness of the day
        cf = float(np.clip(c + rng.normal(0, 0.15), 0, 1))  # what the forecast believed
        for h in range(24):
            t = datetime(d.year, d.month, d.day, h)
            clear = max(0.0, 1050 * math.sin(math.pi * (h - 6.5) / 12)) if 6.5 < h < 18.5 else 0.0
            a_g = max(0.0, clear * (1 - 0.75 * c) * (1 + rng.normal(0, 0.04)))
            f_g = clear * (1 - 0.55 * cf)                   # forecast is optimistic on cloudy days
            a_T = 25.5 + 4 * max(0.0, math.sin(math.pi * (h - 7) / 14)) + 2 * (1 - c) + rng.normal(0, 0.4)
            A[t] = (a_T, c * 100, a_g)
            F[t] = (a_T + rng.normal(0, 0.8), cf * 100, f_g)
        d += timedelta(days=1)
    return F, A


# ---------- synthetic demand history ----------
WEEKEND = {"campus": (0.75, 0.65), "factory": (0.65, 0.50)}   # Saturday, Sunday factors (assumption)


def normalise(out):
    """Scale so a typical weekday's highest hour is 1.0 (the dashboard multiplies by the peak setting)."""
    by_day = {}
    for t, v in out.items():
        by_day.setdefault(t.date(), []).append(v)
    peaks = [max(v) for d, v in by_day.items() if d.weekday() < 5 and len(v) >= 20]
    m = float(np.median(peaks)) if peaks else max(out.values())
    return {t: v / m for t, v in out.items()}


def synth_truth(times, A, prof, seed):
    rng = np.random.default_rng(seed)
    shape = fc.SHAPES[prof]
    out = {}
    for t in times:
        d = t.weekday()
        f = 1.0 if d < 5 else WEEKEND[prof][d - 5]
        cool = 1 + 0.012 * max(A[t][0] - 27, 0) * (1.0 if 8 <= t.hour < 20 else 0.3)
        x = shape[t.hour] * f * cool * (1 + rng.normal(0, 0.03))
        if rng.random() < 0.01:
            x *= rng.choice([0.7, 1.2])                      # odd days: outage, event
        out[t] = max(x, 0.05)
    return normalise(out)


def meter_truth(path):
    buckets = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["timestamp"].strip())
            buckets.setdefault(ts.replace(minute=0, second=0, microsecond=0), []).append(float(row["kw"]))
    return normalise({t: float(np.mean(v)) for t, v in buckets.items()})


# ---------- solar stage ----------
def solar_stage(times, F, A, holdout):
    cut = sorted({t.date() for t in times})[-holdout]
    tr = [t for t in times if t.date() < cut]
    te = [t for t in times if t.date() >= cut]
    col = lambda ts, D, i: np.array([D[t][i] for t in ts])
    PVA = {t: float(fc.pv_per_kwp([A[t][2]], [A[t][0]])[0]) for t in times}   # PV "measured" from actual weather

    def fit(ts):
        f, c, a = col(ts, F, 2), col(ts, F, 1), col(ts, A, 2)
        m = f > 5
        return fc.ridge_fit(fc.solar_features(f[m], c[m]), a[m] / 1000.0, lam=1e-3, free_first=True)

    model = {"w": fit(tr).tolist()}
    f, c, ft = col(te, F, 2), col(te, F, 1), col(te, F, 0)
    true = np.array([PVA[t] for t in te])
    raw = fc.pv_per_kwp(f, ft)
    cor = fc.pv_per_kwp(fc.correct_ghi(model, f, c), ft)
    naive = np.array([PVA.get(t - timedelta(days=1), np.nan) for t in te])
    day = np.array([7 <= t.hour <= 18 for t in te]) & ~np.isnan(naive)
    mae = lambda p: round(float(100 * np.mean(np.abs(p[day] - true[day]))), 2)

    errs = {"model": [], "naive": []}
    for d in sorted({t.date() for t in te}):
        idx = [i for i, t in enumerate(te) if t.date() == d]
        if np.isnan(naive[idx]).any() or true[idx].sum() <= 0:
            continue
        errs["model"].append(abs(cor[idx].sum() - true[idx].sum()) / true[idx].sum())
        errs["naive"].append(abs(naive[idx].sum() - true[idx].sum()) / true[idx].sum())

    ok = cor > 0.2
    p10, p90 = np.percentile(true[ok] / cor[ok], [10, 90]) if ok.sum() > 20 else (0.7, 1.2)
    metrics = {"naive_mae_pct": mae(np.nan_to_num(naive)), "raw_mae_pct": mae(raw), "model_mae_pct": mae(cor),
               "naive_day_energy_err_pct": round(100 * float(np.mean(errs["naive"])), 1),
               "model_day_energy_err_pct": round(100 * float(np.mean(errs["model"])), 1),
               "holdout_days": holdout, "unit": "% of installed kWp, hours 07-18"}
    final = {"w": fit(times).tolist(), "band": {"p10": round(float(p10), 3), "p90": round(float(p90), 3)},
             "metrics": metrics}
    return final


# ---------- demand stage ----------
def demand_stage(times, F, truth, holdout):
    ts = [t for t in times if t in truth]
    cut = sorted({t.date() for t in ts})[-holdout]
    tr = [t for t in ts if t.date() < cut]
    te = [t for t in ts if t.date() >= cut]
    X = lambda L: fc.demand_features(L, [F[t][0] for t in L])
    w = fc.ridge_fit(X(tr), np.array([truth[t] for t in tr]), lam=1e-2)
    pred = X(te) @ w
    true = np.array([truth[t] for t in te])
    wk = np.array([truth.get(t - timedelta(days=7), np.nan) for t in te])
    yd = np.array([truth.get(t - timedelta(days=1), np.nan) for t in te])
    ok = ~np.isnan(wk) & ~np.isnan(yd)
    mae = lambda p: round(float(100 * np.mean(np.abs(p[ok] - true[ok]))), 2)

    pk = {"model": [], "naive": []}
    for d in sorted({t.date() for t in te}):
        idx = [i for i, t in enumerate(te) if t.date() == d]
        if len(idx) < 24 or np.isnan(wk[idx]).any():
            continue
        pk["model"].append(abs(pred[idx].max() - true[idx].max()))
        pk["naive"].append(abs(wk[idx].max() - true[idx].max()))
    metrics = {"model_mae_pct": mae(pred), "naive_mae_pct": mae(wk), "yesterday_mae_pct": mae(yd),
               "model_peak_err_pct": round(100 * float(np.mean(pk["model"])), 2),
               "naive_peak_err_pct": round(100 * float(np.mean(pk["naive"])), 2),
               "holdout_days": holdout, "unit": "% of peak demand",
               "naive": "same hour, same weekday, last week"}
    wf = fc.ridge_fit(X(ts), np.array([truth[t] for t in ts]), lam=1e-2)
    return {"w": wf.tolist(), "metrics": metrics}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=5.41)
    ap.add_argument("--lon", type=float, default=100.33)
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--holdout", type=int, default=28)
    ap.add_argument("--offline", action="store_true", help="use mock weather (self-test only)")
    ap.add_argument("--meter", help="CSV with columns timestamp,kw (real demand history)")
    ap.add_argument("--meter-as", default="campus", choices=["campus", "factory"])
    ap.add_argument("--out", default=fc.MODEL_PATH)
    a = ap.parse_args()

    end = (datetime(2026, 9, 30) if a.offline else datetime.now(fc.KL).replace(tzinfo=None) - timedelta(days=7)).date()
    start = end - timedelta(days=a.days - 1)
    if a.offline:
        F, A = mock_weather(start, end)
        print("OFFLINE self-test with mock weather: numbers below are NOT real.")
    else:
        print(f"Downloading Open-Meteo history {start} to {end} ...")
        F = fc.fetch_hist_forecast(a.lat, a.lon, start, end)
        A = fc.fetch_archive(a.lat, a.lon, start, end)

    both = set(F) & set(A)
    full = [d for d in sorted({t.date() for t in both})
            if all(datetime(d.year, d.month, d.day, h) in both for h in range(24))]
    times = [datetime(d.year, d.month, d.day, h) for d in full for h in range(24)]
    if len(full) < a.holdout + 30:
        sys.exit(f"Only {len(full)} complete days of weather; need at least {a.holdout + 30}.")
    print(f"{len(full)} complete days, last {a.holdout} held back for testing.\n")

    out = {"trained": str(datetime.now(fc.KL).date()), "lat": a.lat, "lon": a.lon,
           "solar": solar_stage(times, F, A, a.holdout), "demand": {}}
    if a.meter:
        out["demand"][a.meter_as] = demand_stage(times, F, meter_truth(a.meter), a.holdout)
        out["data_note"] = NOTE_METER
    else:
        for i, prof in enumerate(("campus", "factory")):
            out["demand"][prof] = demand_stage(times, F, synth_truth(times, A, prof, seed=10 + i), a.holdout)
        out["data_note"] = NOTE_SYNTH
    if a.offline:
        out["data_note"] = "OFFLINE SELF-TEST MODEL, do not deploy. " + out["data_note"]

    s = out["solar"]["metrics"]
    print("SOLAR (error on the held-back days, lower is better)")
    print(f"  yesterday's output      MAE {s['naive_mae_pct']:5.2f}%   daily energy error {s['naive_day_energy_err_pct']:.1f}%")
    print(f"  weather forecast only   MAE {s['raw_mae_pct']:5.2f}%")
    print(f"  forecast + learned fix  MAE {s['model_mae_pct']:5.2f}%   daily energy error {s['model_day_energy_err_pct']:.1f}%")
    for prof, d in out["demand"].items():
        m = d["metrics"]
        print(f"\nDEMAND {prof} (error as % of peak demand)")
        print(f"  same as last week       MAE {m['naive_mae_pct']:5.2f}%   daily peak error {m['naive_peak_err_pct']:.2f}%")
        print(f"  same as yesterday       MAE {m['yesterday_mae_pct']:5.2f}%")
        print(f"  ridge regression        MAE {m['model_mae_pct']:5.2f}%   daily peak error {m['model_peak_err_pct']:.2f}%")
    print("\n" + out["data_note"])
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"\nSaved {a.out}")


if __name__ == "__main__":
    main()
