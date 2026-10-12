"""Solar and demand forecasts for the facility planner (numpy + standard library only).

Solar : Open-Meteo weather forecast -> learned correction of irradiance -> PV physics.
Demand: calendar + temperature regression (ridge), trained offline by tools/forecast_lab.py.

The learned weights live in model.json next to this file. If it is missing the endpoint
still works (physics-only solar, built-in demand shape) and says so in its response.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

KL = timezone(timedelta(hours=8))
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.json")
HOURLY = "temperature_2m,cloud_cover,shortwave_radiation"
URL_FORECAST = "https://api.open-meteo.com/v1/forecast"
URL_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
URL_HIST_FORECAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# PV physics (typical crystalline-silicon module, tilt ignored: GHI is used as plane irradiance)
NOCT = 45.0        # deg C, nominal operating cell temperature
GAMMA = -0.0035    # power change per deg C above 25 C cell temperature
SYS_EFF = 0.88     # inverter, wiring, soiling, mismatch combined (assumption)

# Same weekday shapes as the dashboard (kW, kW-relative), used when no trained model exists
_CAMP = [350, 345, 345, 345, 345, 345, 380, 450, 700, 850, 880, 900, 780, 970, 1050, 1010, 880, 750, 600, 500, 420, 380, 360, 355]
_FAC = [.4, .4, .4, .4, .4, .4, .6, .6, 1, 1, 1, 1, .9, 1, 1, 1, 1, 1, .7, .7, .7, .7, .45, .45]
SHAPES = {"campus": [v / max(_CAMP) for v in _CAMP], "factory": [v / max(_FAC) for v in _FAC]}


class ForecastUnavailable(Exception):
    """Weather service could not be reached or returned incomplete data."""


# ---------- weather ----------
def _fetch(url, params):
    params = dict(params, hourly=HOURLY, timezone="Asia/Kuala_Lumpur")
    req = Request(url + "?" + urlencode(params), headers={"User-Agent": "facility-energy-planner"})
    try:
        with urlopen(req, timeout=20) as r:
            h = json.loads(r.read().decode("utf-8"))["hourly"]
        out = {}
        for t, T, C, G in zip(h["time"], h["temperature_2m"], h["cloud_cover"], h["shortwave_radiation"]):
            if T is None or C is None or G is None:
                continue
            out[datetime.strptime(t, "%Y-%m-%dT%H:%M")] = (float(T), float(C), float(G))
        return out
    except Exception as e:
        raise ForecastUnavailable(f"weather service error: {e}") from e


def fetch_forecast(lat, lon):
    return _fetch(URL_FORECAST, {"latitude": lat, "longitude": lon, "forecast_days": 3})


def fetch_archive(lat, lon, start, end):          # reanalysis, used as the "actual" weather
    return _fetch(URL_ARCHIVE, {"latitude": lat, "longitude": lon, "start_date": str(start), "end_date": str(end)})


def fetch_hist_forecast(lat, lon, start, end):    # what the forecast said at the time
    return _fetch(URL_HIST_FORECAST, {"latitude": lat, "longitude": lon, "start_date": str(start), "end_date": str(end)})


# ---------- small maths helpers ----------
def ridge_fit(X, y, lam=1e-2, free_first=False):
    """Least-squares fit with a small penalty on the weights (stops over-fitting)."""
    P = np.eye(X.shape[1]) * lam
    if free_first:
        P[0, 0] = 0.0
    return np.linalg.solve(X.T @ X + P, X.T @ y)


def pv_per_kwp(ghi, tamb):
    """kW produced per kWp installed, from irradiance (W/m2) and ambient temperature (C)."""
    ghi = np.asarray(ghi, float)
    tcell = np.asarray(tamb, float) + (NOCT - 20.0) / 800.0 * ghi
    return np.clip(ghi / 1000.0 * (1.0 + GAMMA * (tcell - 25.0)) * SYS_EFF, 0.0, 1.0)


def solar_features(ghi, cloud_pct):
    g = np.asarray(ghi, float) / 1000.0
    c = np.asarray(cloud_pct, float) / 100.0
    return np.column_stack([np.ones_like(g), g, g * c, g * g])


def correct_ghi(model, ghi, cloud_pct):
    """Apply the learned correction to forecast irradiance. Night stays at zero."""
    ghi = np.asarray(ghi, float)
    if not model:
        return ghi
    out = solar_features(ghi, cloud_pct) @ np.array(model["w"]) * 1000.0
    return np.clip(np.where(ghi > 5, out, 0.0), 0.0, 1400.0)


def demand_features(times, temps):
    """Hour-of-day x (weekday / Saturday / Sunday) pattern, plus cooling load above 27 C."""
    X = np.zeros((len(times), 24 * 3 + 2))
    for i, (dt, T) in enumerate(zip(times, temps)):
        d = dt.weekday()
        X[i, (0 if d < 5 else 1 if d == 5 else 2) * 24 + dt.hour] = 1.0
        cdd = max(float(T) - 27.0, 0.0)
        X[i, 72 if 8 <= dt.hour < 20 else 73] = cdd
    return X


# ---------- public entry point ----------
_MODEL = {"loaded": False, "data": None}


def load_model():
    if not _MODEL["loaded"]:
        try:
            with open(MODEL_PATH, "r", encoding="utf-8") as f:
                _MODEL["data"] = json.load(f)
        except (OSError, ValueError):
            _MODEL["data"] = None
        _MODEL["loaded"] = True
    return _MODEL["data"]


def _num(q, key, lo, hi, default):
    try:
        x = float(q.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return x


def forecast(q, weather=None, now=None):
    """q: dict of query parameters. Returns 24 hourly values for the chosen day (kW)."""
    lat = _num(q, "lat", -90, 90, 5.41)       # default: Penang
    lon = _num(q, "lon", -180, 180, 100.33)
    kwp = _num(q, "kwp", 0, 1e5, 100)
    peak = _num(q, "peak", 0, 1e6, 1000)
    day = int(_num(q, "day", 0, 2, 1))        # 0 today, 1 tomorrow, 2 day after
    prof = q.get("prof", "campus")
    if prof not in SHAPES:
        raise ValueError("prof must be campus or factory")

    target = ((now or datetime.now(KL)) + timedelta(days=day)).date()
    wx = weather if weather is not None else fetch_forecast(lat, lon)
    rows = [wx.get(datetime(target.year, target.month, target.day, h)) for h in range(24)]
    if any(r is None for r in rows):
        raise ForecastUnavailable("incomplete weather data for " + str(target))
    temp, cloud, ghi = (np.array([r[i] for r in rows]) for i in range(3))

    model = load_model() or {}
    sm = model.get("solar")
    ghi_c = correct_ghi(sm, ghi, cloud)
    pv = kwp * pv_per_kwp(ghi_c, temp)
    band = (sm or {}).get("band")

    times = [datetime(target.year, target.month, target.day, h) for h in range(24)]
    dm = (model.get("demand") or {}).get(prof)
    if dm:
        pu = np.clip(demand_features(times, temp) @ np.array(dm["w"]), 0.05, 1.5)
    else:
        pu = np.array(SHAPES[prof])
    load = pu * peak

    r3 = lambda a: [round(float(v), 3) for v in a]
    return {
        "date": str(target), "source": "open-meteo", "lat": lat, "lon": lon,
        "pv": r3(pv),
        "pv_p10": r3(pv * band["p10"]) if band else None,
        "pv_p90": r3(pv * band["p90"]) if band else None,
        "load": r3(load),
        "ghi": r3(ghi), "temp": r3(temp), "cloud": r3(cloud),
        "model": {"pv": "learned correction + physics" if sm else "physics only",
                  "demand": "ridge regression" if dm else "built-in profile",
                  "trained": model.get("trained"), "data_note": model.get("data_note")},
        "metrics": {"pv": (sm or {}).get("metrics"), "load": (dm or {}).get("metrics")},
    }
