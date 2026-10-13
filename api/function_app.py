import json
import logging
import time

import azure.functions as func

from optimiser import optimise

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


@app.route(route="audit", methods=["GET"])
def audit_http(req: func.HttpRequest) -> func.HttpResponse:
    """Latest logged calls, newest first. Reachable only after sign-in (see staticwebapp.config.json)."""
    if audit is None:
        return _json({"enabled": False, "items": []})
    return _json({"enabled": audit.enabled(), "items": audit.recent(15)})
