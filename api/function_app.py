import json
import azure.functions as func
from optimiser import optimise
from forecast import forecast, ForecastUnavailable

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="optimise", methods=["POST"])
def optimise_http(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        result = optimise(body)
        return func.HttpResponse(json.dumps(result), mimetype="application/json")
    except (ValueError, TypeError, KeyError) as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=400,
                                 mimetype="application/json")
    except Exception:
        return func.HttpResponse(json.dumps({"error": "optimisation failed"}),
                                 status_code=500, mimetype="application/json")


@app.route(route="forecast", methods=["GET"])
def forecast_http(req: func.HttpRequest) -> func.HttpResponse:
    """Tomorrow's solar and demand forecast: /api/forecast?kwp=300&peak=1050&prof=campus&day=1"""
    try:
        result = forecast(dict(req.params))
        return func.HttpResponse(json.dumps(result), mimetype="application/json")
    except ValueError as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=400,
                                 mimetype="application/json")
    except ForecastUnavailable as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=503,
                                 mimetype="application/json")
    except Exception:
        return func.HttpResponse(json.dumps({"error": "forecast failed"}),
                                 status_code=500, mimetype="application/json")
