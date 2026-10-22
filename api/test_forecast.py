"""Checks for the forecast module. Run:  python test_forecast.py   (needs numpy)"""
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import forecast as fc

PEN, KL = (5.41, 100.33), (3.0553, 101.7003)
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=fc.KL)


def wx(day=1, ghi_peak=900.0, temp=30.0, cloud=40.0):
    """A clean synthetic day of weather for the target date."""
    t0 = (NOW + timedelta(days=day)).date()
    out = {}
    for h in range(24):
        g = max(0.0, ghi_peak * math.sin(math.pi * (h - 6.5) / 12)) if 6.5 < h < 18.5 else 0.0
        out[datetime(t0.year, t0.month, t0.day, h)] = (temp, cloud, g)
    return out


SOLAR = {"w": [0.0, 1.0, 0.0, 0.0], "band": {"p10": 0.7, "p90": 1.2},
         "metrics": {"raw_mae_pct": 6.0, "model_mae_pct": 5.0}}
MODEL_SITES = {"trained": "2026-10-13", "sites": [{"name": "Penang", "lat": PEN[0], "lon": PEN[1], "solar": SOLAR}], "demand": {}}


def with_model(model):
    old = fc._MODEL.copy()
    fc._MODEL.update(loaded=True, data=model)
    return old


def run(loc, model, **kw):
    old = with_model(model)
    try:
        q = dict(lat=loc[0], lon=loc[1], kwp=100, peak=1000, day=1, prof="campus")
        q.update(kw)
        return fc.forecast(q, weather=wx(), now=NOW)
    finally:
        fc._MODEL.update(old)


def test_distance():
    assert abs(fc.km_between(*PEN, *PEN)) < 1e-9
    d = fc.km_between(*PEN, *KL)
    assert 290 < d < 340, d          # Penang to Kuala Lumpur is roughly 300 km


def test_pick_site():
    s, km = fc.pick_site(MODEL_SITES, *PEN)
    assert s and s["name"] == "Penang" and km < 1
    s, km = fc.pick_site(MODEL_SITES, 5.45, 100.30)      # a few km away: still Penang
    assert s is not None
    s, km = fc.pick_site(MODEL_SITES, *KL)                # ~300 km away: refused
    assert s is None and km > 250
    assert fc.pick_site(None, *PEN) == (None, None)
    assert fc.pick_site({"sites": []}, *PEN) == (None, None)


def test_legacy_single_site_layout_still_works():
    legacy = {"lat": PEN[0], "lon": PEN[1], "solar": SOLAR, "demand": {}}
    assert len(fc.model_sites(legacy)) == 1
    assert fc.pick_site(legacy, *PEN)[0] is not None
    assert fc.pick_site(legacy, *KL)[0] is None


def test_model_not_applied_far_away():
    near, far = run(PEN, MODEL_SITES), run(KL, MODEL_SITES)
    assert near["model"]["pv"].startswith("learned correction") and near["model"]["site"]["km"] < 1
    assert far["model"]["site"] is None and "no solar model trained within" in far["model"]["pv"]
    assert far["metrics"]["pv"] is None and far["pv_p10"] is None      # no band or accuracy figures borrowed from another city
    assert near["pv_p10"] is not None


def test_no_model_is_physics_only():
    r = run(PEN, None)
    assert r["model"]["pv"] == "physics only" and r["pv_p10"] is None
    assert len(r["pv"]) == 24 and max(r["pv"]) > 0


def test_pv_physics():
    assert fc.pv_per_kwp([0], [30])[0] == 0
    p = fc.pv_per_kwp([1000], [25])[0]
    assert 0.7 < p < 0.88                                   # hot cells and losses keep it below 1 kW per kWp
    assert fc.pv_per_kwp([1000], [40])[0] < p               # hotter air, less power
    night = fc.pv_per_kwp(np.zeros(24), np.full(24, 28.0))
    assert night.max() == 0


def test_output_shape_and_night():
    r = run(PEN, MODEL_SITES)
    assert len(r["pv"]) == len(r["load"]) == 24
    assert all(v == 0 for v in (r["pv"][h] for h in (0, 1, 2, 22, 23)))
    assert r["date"] == "2026-10-23"


def test_bad_input_rejected():
    for bad in ({"lat": 95}, {"kwp": -1}, {"day": 9}, {"prof": "mall"}, {"peak": "abc"}, {"lon": float("nan")}):
        try:
            run(PEN, MODEL_SITES, **bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_incomplete_weather():
    w = wx()
    w.pop(sorted(w)[5])
    try:
        fc.forecast(dict(lat=PEN[0], lon=PEN[1]), weather=w, now=NOW)
    except fc.ForecastUnavailable:
        return
    raise AssertionError("incomplete weather accepted")


def test_cache_and_stale_fallback():
    fc._WX.clear()
    calls = []

    def ok(lat, lon):
        calls.append((lat, lon))
        return {"x": 1}

    def down(lat, lon):
        raise fc.ForecastUnavailable("down")

    d, i = fc.fetch_forecast_cached(*PEN, now_s=1000, fetcher=ok)
    assert not i["cached"] and len(calls) == 1
    d, i = fc.fetch_forecast_cached(*PEN, now_s=1000 + 600, fetcher=ok)
    assert i["cached"] and not i["stale"] and len(calls) == 1
    d, i = fc.fetch_forecast_cached(*PEN, now_s=1000 + 3600, fetcher=down)      # service down: reuse old copy, flagged
    assert i["stale"]
    try:
        fc.fetch_forecast_cached(*PEN, now_s=1000 + 7 * 3600, fetcher=down)     # too old to reuse
    except fc.ForecastUnavailable:
        pass
    else:
        raise AssertionError("served a copy older than 6 h")
    fc.fetch_forecast_cached(*KL, now_s=2000, fetcher=ok)                        # a different place gets its own entry
    assert (3.06, 101.7) in [(round(a, 2), round(b, 2)) for a, b in calls]
    fc._WX.clear()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} checks passed")
