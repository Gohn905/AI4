import json
import azure.functions as func
from optimiser import optimise

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
