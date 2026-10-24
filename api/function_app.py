import json
import logging
import time

import azure.functions as func

from optimiser import optimise
import reference
import reopt

# Optional features must never stop the optimiser from starting, even if a file is missing or broken.
try:
    import audit
except Exception as e:                      # pragma: no cover
    logging.warning("audit module unavailable: %s", e)
    audit = None
try:
    from forecast import forecast, ForecastUnavailable
except Exception as e:                      # pragma: no cover
    logging.warning("forecast module unavailable: %s", e)
    forecast = None

    class ForecastUnavailable(Exception):
        pass


try:
    import profiles
except Exception as e:                      # pragma: no cover
    logging.warning("profiles module unavailable: %s", e)
    profiles = None
try:
    import explain
except Exception as e:                      # pragma: no cover
    logging.warning("explain module unavailable: %s", e)
    explain = None
try:
    import assist
except Exception as e:                      # pragma: no cover
    logging.warning("assist module unavailable: %s", e)
    assist = None


def _log(*args, **kwargs):
    if audit is not None:
        try:
            audit.record(*args, **kwargs)
        except Exception:
            pass

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _json(obj, status=200):
    return func.HttpResponse(json.dumps(obj), status_code=status, mimetype="application/json")


@app.route(route="optimise", methods=["POST"])
def optimise_http(req: func.HttpRequest) -> func.HttpResponse:
    t0, body = time.perf_counter(), None
    ms = lambda: (time.perf_counter() - t0) * 1000
    try:
        body = req.get_json()
        result = optimise(body)
        _log("optimise", req, 200, ms(), model="linear program (HiGHS)",
                     request={k: body.get(k) for k in ("batt_kwh", "flex_share", "carbon_adder", "grid_limit", "wear", "md_rate_day")},
                     result={k: result[k] for k in ("cost", "peak", "grid", "ch", "dis", "flex")},
                     summary=f"cost RM{result['cost']:.2f}, billed peak {result['peak']:.0f} kW")
        return _json(result)
    except (ValueError, TypeError, KeyError) as e:
        _log("optimise", req, 400, ms(), summary="rejected: " + str(e))
        return _json({"error": str(e)}, 400)
    except Exception:
        _log("optimise", req, 500, ms(), summary="optimisation failed")
        return _json({"error": "optimisation failed"}, 500)


def _kpi_summary(x):
    return {k: v for k, v in x.items() if not isinstance(v, list)}


@app.route(route="compare", methods=["POST"])
def compare_http(req: func.HttpRequest) -> func.HttpResponse:
    """Same optimiser request as /api/optimise. Returns KPIs for solar only, battery with simple rules, and the optimiser, all on identical inputs."""
    t0 = time.perf_counter()
    ms = lambda: (time.perf_counter() - t0) * 1000
    try:
        res = reference.compare(req.get_json(), series=False)
        _log("compare", req, 200, ms(), model="reference strategies + HiGHS",
             summary=f"optimised vs best reference {res['savings']['optimised_vs_best_reference_pct']:.1f}%")
        return _json(res)
    except (ValueError, TypeError, KeyError) as e:
        _log("compare", req, 400, ms(), summary="rejected: " + str(e))
        return _json({"error": str(e)}, 400)
    except Exception:
        _log("compare", req, 500, ms(), summary="comparison failed")
        return _json({"error": "comparison failed"}, 500)


@app.route(route="reoptimise", methods=["POST"])
def reoptimise_http(req: func.HttpRequest) -> func.HttpResponse:
    """Re-optimisation SIMULATION: optimiser request plus scenario (or actual) and replan settings. See reopt.py."""
    t0 = time.perf_counter()
    ms = lambda: (time.perf_counter() - t0) * 1000
    try:
        res = reopt.simulate(req.get_json())
        _log("reoptimise", req, 200, ms(), model="rolling-horizon simulation (HiGHS)",
             summary=f"{len(res['replans'])} re-plans, rolling vs open-loop RM{res['summary']['rolling_vs_open_loop_rm']:.0f}")
        return _json(res)
    except (ValueError, TypeError, KeyError) as e:
        _log("reoptimise", req, 400, ms(), summary="rejected: " + str(e))
        return _json({"error": str(e)}, 400)
    except Exception:
        _log("reoptimise", req, 500, ms(), summary="simulation failed")
        return _json({"error": "simulation failed"}, 500)


@app.route(route="forecast", methods=["GET"])
def forecast_http(req: func.HttpRequest) -> func.HttpResponse:
    """Tomorrow's solar and demand forecast: /api/forecast?kwp=300&peak=1050&prof=campus&day=1"""
    t0, q = time.perf_counter(), dict(req.params)
    ms = lambda: (time.perf_counter() - t0) * 1000
    try:
        if forecast is None:
            raise ForecastUnavailable("forecast module not installed")
        result = forecast(q)
        m = result["model"]
        _log("forecast", req, 200, ms(), request=q, model=f"solar: {m['pv']}; demand: {m['demand']}",
                     result={"date": result["date"], "pv": result["pv"], "load": result["load"], "model": m},
                     summary=f"{result['date']}, solar peak {max(result['pv']):.0f} kW, load peak {max(result['load']):.0f} kW")
        return _json(result)
    except ValueError as e:
        _log("forecast", req, 400, ms(), request=q, summary="rejected: " + str(e))
        return _json({"error": str(e)}, 400)
    except ForecastUnavailable as e:
        _log("forecast", req, 503, ms(), request=q, summary=str(e))
        return _json({"error": str(e)}, 503)
    except Exception:
        _log("forecast", req, 500, ms(), request=q, summary="forecast failed")
        return _json({"error": "forecast failed"}, 500)


@app.route(route="explain", methods=["POST"])
def explain_http(req: func.HttpRequest) -> func.HttpResponse:
    """Plain-language summary of one day's plan. The model only narrates numbers; see explain.py."""
    t0 = time.perf_counter()
    ms = lambda: (time.perf_counter() - t0) * 1000
    if explain is None:
        return _json({"error": "explanations are not available"}, 503)
    body = None
    try:
        if len(req.get_body() or b"") > 4000:
            return _json({"error": "request too large"}, 413)
        body = req.get_json()
        out = explain.explain(body)
        _log("explain", req, 200, ms(), request=body, result=out, model=out["source"],
             summary=out["source"] + (": " + out["note"] if out["note"] else ""))
        return _json(out)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        _log("explain", req, 400, ms(), summary="rejected: " + str(e))
        return _json({"error": str(e)}, 400)
    except Exception:
        _log("explain", req, 500, ms(), summary="explain failed")
        return _json({"error": "explain failed"}, 500)


@app.route(route="assist", methods=["POST"])
def assist_http(req: func.HttpRequest) -> func.HttpResponse:
    """Setup and navigation assistant. The model only proposes; assist.py checks every value. See assist.py."""
    t0 = time.perf_counter()
    ms = lambda: (time.perf_counter() - t0) * 1000
    if assist is None:
        return _json({"error": "the assistant is not available"}, 503)
    try:
        if len(req.get_body() or b"") > 14000:
            return _json({"error": "request too large"}, 413)
        body = req.get_json()
        out = assist.assist(body)
        # privacy: the text the person typed or pasted is never written to the audit log
        _log("assist", req, 200, ms(), request={"chars": len(str(body.get("message", ""))), "document": bool(body.get("document"))},
             result={k: out.get(k) for k in ("facility", "navigate")}, model="ai",
             summary="proposed: " + ", ".join(k for k in ("facility", "navigate") if out.get(k)) if (out.get("facility") or out.get("navigate")) else "reply only")
        return _json(out)
    except assist.Unavailable as e:
        _log("assist", req, 503, ms(), summary=str(e))
        return _json({"error": str(e)}, 503)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        _log("assist", req, 400, ms(), summary="rejected: " + str(e))
        return _json({"error": str(e)}, 400)
    except Exception:
        _log("assist", req, 500, ms(), summary="assist failed")
        return _json({"error": "assist failed"}, 500)


@app.route(route="audit", methods=["GET"])
def audit_http(req: func.HttpRequest) -> func.HttpResponse:
    """Latest logged calls, newest first. Reachable only after sign-in (see staticwebapp.config.json)."""
    if audit is None:
        return _json({"enabled": False, "items": []})
    return _json({"enabled": audit.enabled(), "items": audit.recent(15)})


@app.route(route="profile", methods=["GET", "PUT", "DELETE"])
def profile_http(req: func.HttpRequest) -> func.HttpResponse:
    """Private saved sites. Needs sign-in: the owner id comes from the platform header, never from the request body."""
    t0 = time.perf_counter()
    ms = lambda: (time.perf_counter() - t0) * 1000
    if profiles is None:
        return _json({"error": "saved sites are not available"}, 503)
    u = profiles.uid_of(req)
    if not u:
        return _json({"error": "sign in required"}, 401)
    try:
        if len(req.get_body() or b"") > 12000:
            return _json({"error": "request too large"}, 413)
        site = req.params.get("site")
        if req.method == "GET":
            return _json({"name": site, "data": profiles.get_site(u, site)} if site else {"sites": profiles.list_sites(u)})
        if req.method == "PUT":
            b = req.get_json()
            profiles.put_site(u, b.get("name"), b.get("data"))
            _log("profile", req, 200, ms(), summary="site saved")
            return _json({"saved": True, "sites": profiles.list_sites(u)})
        if req.params.get("all") == "1":
            n = profiles.delete_all(u)
            _log("profile", req, 200, ms(), summary=f"all saved data deleted ({n})")
            return _json({"deleted": n, "sites": []})
        profiles.delete_site(u, site)
        _log("profile", req, 200, ms(), summary="site deleted")
        return _json({"deleted": 1, "sites": profiles.list_sites(u)})
    except profiles.NotFound as e:
        return _json({"error": str(e)}, 404)
    except profiles.Unavailable as e:
        return _json({"error": str(e)}, 503)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        return _json({"error": str(e)}, 400)
    except Exception:
        logging.exception("profile failed")
        return _json({"error": "profile request failed"}, 500)
