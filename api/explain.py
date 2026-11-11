"""Plain-language explanation of one day's plan.

The language model only NARRATES numbers that the optimiser already produced. It never changes them.
Safety design:
  1. The request holds numbers and a few fixed words only (no free text), so there is nothing to inject.
  2. The server turns the numbers into fixed phrases ("RM 6,325"); only those phrases go to the model.
  3. Every number in the model's answer must appear in those phrases. If not, the answer is thrown away
     and a fixed template sentence set is returned instead (source = "template").
  4. If the model is not configured, slow, busy or errors, the same template text is returned.
App settings (Static Web App or Function App):
  AZURE_OPENAI_ENDPOINT    e.g. https://<resource>.openai.azure.com
  AZURE_OPENAI_KEY         API key (secret), OR set AZURE_OPENAI_USE_IDENTITY=1 to use the managed identity (see aoai.py)
  AZURE_OPENAI_DEPLOYMENT  the deployment name, e.g. Sunshine_explain
  AZURE_OPENAI_REASONING   optional, default "minimal" (set empty to omit)
"""
import json
import logging
import os
import re
import time

import aoai
from urllib.error import HTTPError
from urllib.request import Request, urlopen

STRATS = ("Baseline", "Cost-saving", "Balanced", "Production-first", "Lowest-carbon")
TARIFFS = ("LV General", "LV Time-of-Use", "MV General", "MV Time-of-Use", "HV General", "HV Time-of-Use")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# A time of day on the half hour (00:00 to 23:30, or 24:00 for the end of the day) and 1 to 24 runs of "start to end".
# The plan has 48 half-hour steps, so a day can hold at most 24 separate runs. The pattern is strict on purpose: it is the
# only free-text-like input that reaches the model prompt.
_T = r"(?:(?:[01]\d|2[0-3]):[03]0|24:00)"
RUN = re.compile(rf"^{_T} to {_T}(?:, {_T} to {_T}){{0,23}}$")
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

SYSTEM = ("You explain one day's energy plan to a facility manager in plain English. "
          "Write exactly 3 short sentences, under 80 words, no lists, no markdown. "
          "Use ONLY the numbers in the JSON, copied exactly as written (keep RM, kW, %, and HH:00 times). "
          "Never calculate, round, convert or invent a number, and never mention dates. "
          "If a field is missing, do not mention it. If the saving is RM 0, say plainly that this strategy "
          "saves nothing on this day and give the reason from the fields. "
          "Do not state differences, reductions, totals, durations or counts of hours: only the values given. "
          "Do not give advice beyond the plan. Do not mention the JSON. "
          'Example. Data: {"day": "Tuesday", "strategy": "Cost-saving", "baseline_cost": "RM 2,000", "plan_cost": "RM 1,800", '
          '"saving": "RM 200", "saving_percent": "10.0%", "billed_peak_before": "500 kW", "billed_peak_after": "400 kW"} '
          "Answer: On Tuesday, Cost-saving lowers the day's cost from RM 2,000 to RM 1,800, a saving of RM 200 (10.0%). "
          "The billed peak goes from 500 kW to 400 kW.")


class Unavailable(Exception):
    pass


def _detail(e, raw):
    """Short, safe text from the AI service's error, for the note and the log (long key-like strings removed)."""
    t = re.sub(r"[A-Za-z0-9_\-]{32,}", "[hidden]", " ".join(raw.split()))[:200]
    logging.warning("AI model HTTP %s: %s", e.code, t)
    return t


def _num(f, key, lo, hi):
    try:
        x = float(f[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(key + " must be a number")
    if x != x or not lo <= x <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return x


def validate(f):
    if not isinstance(f, dict):
        raise ValueError("body must be an object")
    out = {}
    for k, opts in (("day", DAYS), ("strategy", STRATS), ("tariff", TARIFFS)):
        if f.get(k) not in opts:
            raise ValueError(k + " has an unknown value")
        out[k] = f[k]
    out["billed"] = bool(f.get("billed"))
    out["base_cost"] = _num(f, "base_cost", 0, 1e9)
    out["plan_cost"] = _num(f, "plan_cost", 0, 1e9)
    out["base_peak"] = _num(f, "base_peak", 0, 1e7)
    out["plan_peak"] = _num(f, "plan_peak", 0, 1e7)
    out["solar_pct"] = _num(f, "solar_pct", 0, 100)
    out["co2_pct"] = None if f.get("co2_pct") is None else _num(f, "co2_pct", -100, 1000)
    for k in ("charge", "discharge", "shift"):
        v = f.get(k) or ""
        if v and not (isinstance(v, str) and RUN.match(v)):
            raise ValueError(k + " must look like 10:00 to 13:00 or 09:30 to 10:00 (half hours, at most 24 runs)")
        out[k] = v
    return out


def _money(x):
    return f"RM {x:,.0f}" if abs(x) >= 100 else f"RM {x:.2f}"


def phrases(f):
    """The only wording and numbers the model is allowed to use."""
    sv = f["base_cost"] - f["plan_cost"]
    p = {"day": f["day"], "strategy": f["strategy"], "tariff": f["tariff"], "baseline_cost": _money(f["base_cost"]),
         "plan_cost": _money(f["plan_cost"])}
    if sv >= 0:
        p["saving"] = _money(sv)
        if f["base_cost"] > 0:
            p["saving_percent"] = f"{100 * sv / f['base_cost']:.1f}%"
    else:
        p["extra_cost"] = _money(-sv)
    if f["billed"]:
        p["billed_peak_before"] = f"{f['base_peak']:.0f} kW"
        p["billed_peak_after"] = f"{f['plan_peak']:.0f} kW"
    else:
        p["demand_charge"] = "no maximum demand charge is billed on this day"
    for k, name in (("charge", "battery_charges"), ("discharge", "battery_discharges"), ("shift", "equipment_runs")):
        if f[k]:
            p[name] = f[k]
    p["solar_used"] = f"{f['solar_pct']:.0f}%"
    if f["co2_pct"] is not None and abs(f["co2_pct"]) >= 0.05:
        p["co2_vs_baseline"] = f"{f['co2_pct']:+.1f}%"
    return p


def template(f):
    p = phrases(f)
    if f["strategy"] == "Baseline":
        return (f"Baseline means solar only, with no battery and no load shifting. On {p['day']} it costs "
                f"{p['baseline_cost']} on the {p['tariff']} tariff.")
    s = []
    if "saving" in p and f["base_cost"] - f["plan_cost"] >= 0.5:
        s.append(f"On {p['day']}, {p['strategy']} lowers the day's cost from {p['baseline_cost']} to {p['plan_cost']}, "
                 f"a saving of {p['saving']}" + (f" ({p['saving_percent']})." if "saving_percent" in p else "."))
    elif "extra_cost" in p:
        s.append(f"On {p['day']}, {p['strategy']} costs {p['extra_cost']} more than the baseline of {p['baseline_cost']}.")
    else:
        s.append(f"On {p['day']}, {p['strategy']} saves nothing: the day costs {p['baseline_cost']} either way.")
    if "billed_peak_before" in p:
        s.append(f"The billed peak goes from {p['billed_peak_before']} to {p['billed_peak_after']}.")
    else:
        s.append("No maximum demand charge is billed on this day, so there is no peak to reduce.")
    bits = []
    if "battery_charges" in p:
        bits.append(f"the battery charges {p['battery_charges']}")
    if "battery_discharges" in p:
        bits.append(f"discharges {p['battery_discharges']}")
    if "equipment_runs" in p:
        bits.append(f"flexible equipment runs {p['equipment_runs']}")
    if bits:
        s.append("In the plan, " + ", ".join(bits) + ".")
    return " ".join(s)


def bad_numbers(text, p):
    """Numbers in `text` that are not in the allowed phrases."""
    allowed = {0.0}
    for v in p.values():
        for n in NUM.findall(str(v)):
            allowed.add(float(n.replace(",", "")))
    text = re.sub(r"co\s?[2\u2082]", "co-two", text or "", flags=re.I)      # the 2 in "CO2" is part of a word, not a number
    return [n for n in NUM.findall(text) if float(n.replace(",", "")) not in allowed]


def numbers_ok(text, p):
    """True only if every number in `text` appears in the allowed phrases."""
    if not text or len(text) > 900 or "http" in text.lower() or "```" in text:
        return False
    return not bad_numbers(text, p)


def _endpoint():
    """Only the address of the AI resource (https://name.services.ai.azure.com). Any pasted path such as
    /openai/v1/responses is dropped, because call_model adds the right path itself."""
    from urllib.parse import urlparse
    ep = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    if ep and "://" not in ep:
        ep = "https://" + ep
    u = urlparse(ep)
    return f"{u.scheme}://{u.netloc}" if u.netloc else ""


def call_model(p, fix=None, system=None):
    ep, dep = _endpoint(), os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
    try:
        auth = aoai.auth_headers()
    except Exception as e:
        raise Unavailable("AI sign-in failed: " + type(e).__name__)
    if not (ep and dep and auth):
        raise Unavailable("AI model is not configured")
    if not aoai.reserve():                      # one budget shared with the assistant (see aoai.py)
        raise Unavailable("AI model is busy")
    body = {"model": dep, "max_completion_tokens": 900,
            "messages": [{"role": "system", "content": system or SYSTEM}, {"role": "user", "content": json.dumps(p)}]
            + ([{"role": "assistant", "content": fix["text"]}, {"role": "user", "content": fix["msg"]}] if fix else [])}
    eff = os.environ.get("AZURE_OPENAI_REASONING", "minimal")
    if eff:
        body["reasoning_effort"] = eff
    url = ep + "/openai/v1/chat/completions"
    last, waited = "", False
    for _ in range(4):
        req = Request(url, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", **auth})
        try:
            with urlopen(req, timeout=25) as r:
                j = json.loads(r.read().decode("utf-8"))
            return (j["choices"][0]["message"].get("content") or "").strip()
        except HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            msg = raw[:300]
            last = "[" + url.split("?")[0] + "] " + _detail(e, raw)
            if e.code == 429 and not waited:        # the model asked us to slow down: wait a moment, try once more
                waited = True
                time.sleep(aoai.retry_wait(e))
                continue
            if e.code == 400 and "reasoning_effort" in body and "reasoning" in msg.lower():
                body.pop("reasoning_effort")       # this deployment does not accept it: retry once without
                continue
            if e.code == 404 and "/openai/v1/" in url:     # older resources: try the classic address once
                url = ep + f"/openai/deployments/{dep}/chat/completions?api-version=2025-04-01-preview"
                continue
            if e.code == 404:
                raise Unavailable("AI model error 404: check AZURE_OPENAI_ENDPOINT (it must contain .openai.azure.com) "
                                  "and AZURE_OPENAI_DEPLOYMENT (the exact deployment name)")
            raise Unavailable(f"AI model error {e.code}: {last}" if last else f"AI model error {e.code}")
        except Exception as e:
            raise Unavailable("AI model unreachable: " + type(e).__name__)
    raise Unavailable("AI model rejected the request" + (": " + last if last else ""))


def explain(raw, model=call_model):
    if isinstance(raw, dict) and raw.get("kind") == "replay":
        return explain_replay(raw, None if model is call_model else model)
    f = validate(raw)
    base = {"text": template(f), "source": "template", "note": ""}
    if f["strategy"] == "Baseline":
        return base
    p = phrases(f)
    try:
        text = " ".join(model(p).split())
    except Unavailable as e:
        base["note"] = str(e)
        return base
    except Exception as e:                      # never let the AI break the page
        logging.warning("explain failed: %s", e)
        base["note"] = "AI model error"
        return base
    if not numbers_ok(text, p):
        bad = bad_numbers(text, p)
        logging.warning("explain answer rejected (numbers %s): %s", bad[:5], text[:300])
        if bad:
            fix = {"text": text, "msg": "Your answer used numbers that are not in the data: " + ", ".join(bad[:5]) +
                   ". Rewrite it in 3 short sentences using only numbers copied exactly from the JSON. "
                   "Do not give differences, totals or hour counts."}
            try:
                second = " ".join(model(p, fix).split())
            except Exception:
                second = ""
            if second and numbers_ok(second, p):
                return {"text": second, "source": "ai", "note": ""}
            base["note"] = "AI answer was rejected: it used numbers that are not in the plan (" + ", ".join(bad[:3]) + ")"
        else:
            base["note"] = "AI answer was rejected: it was too long or had a link"
        return base
    return {"text": text, "source": "ai", "note": ""}


# ===================== Live replay review =====================
# Same safety design: numbers in, fixed phrases out, the model only rewrites them, every number in its answer is checked.
# The improvement ideas are chosen by RULES from the run's numbers (below); the model only puts them into plain words.
HM = re.compile(r"^(?:[01]\d|2[0-3]):(?:00|15|30|45)$|^24:00$")
REPLAY_SYSTEM = ("You review one simulated day of a factory energy plan for its operations team, in plain English. "
                 "Write 4 short sentences, under 110 words, no lists, no markdown: first how far the day moved from the original plan, "
                 "then what re-planning recovered, then the improvements given in the JSON field \"improve\", in that order. "
                 "Use ONLY the numbers in the JSON, copied exactly as written (keep RM, kW, %, and HH:MM times). "
                 "Never calculate, round, convert or invent a number. Do not add improvements that are not in \"improve\". "
                 "Say it was a simulation once. Do not mention the JSON.")
IMPROVE = {
    "learn": "press Learn from this run so the forecast is corrected next time, because the solar error repeated",
    "panels": "check the panels for shade or dirt, because solar stayed below forecast",
    "demand": "look for equipment that ran outside the plan, because demand was above forecast",
    "battery": "consider a bigger battery or a higher top-up before the peak window, because the battery reached its reserve",
    "ev": "keep the EV charging window open long enough for held charging to finish, because EV charging was held for solar",
    "twin": "recalibrate the digital twin, because it missed too many intervals",
    "none": "keep the current plan, because the day stayed close to the forecast",
}


def validate_replay(f):
    out = {"strategy": f.get("strategy")}
    if out["strategy"] not in STRATS:
        raise ValueError("strategy has an unknown value")
    for k in ("start", "end"):
        v = f.get(k)
        if not (isinstance(v, str) and HM.match(v)):
            raise ValueError(k + " must be HH:MM on the quarter hour")
        out[k] = v
    for k, lo, hi in (("plan_cost", 0, 1e9), ("kept_cost", 0, 1e9), ("new_cost", 0, 1e9), ("plan_peak", 0, 1e7), ("new_peak", 0, 1e7),
                      ("solar_pct", 0, 1000), ("demand_pct", 0, 1000), ("steps", 1, 96), ("solar_off", 0, 96), ("demand_off", 0, 96),
                      ("grid_off", 0, 96), ("twin_pct", 0, 100), ("ev_held", 0, 96), ("soc_min", 0, 100), ("reserve", 0, 100)):
        out[k] = _num(f, k, lo, hi)
    out["learned"] = bool(f.get("learned"))
    return out


def improve_keys(f):
    k = []
    if f["solar_off"] >= 2 and f["solar_pct"] < 85:
        k.append("panels")
        if f["solar_off"] >= 4 and not f["learned"]:
            k.append("learn")
    if f["demand_off"] >= 2 and f["demand_pct"] > 110:
        k.append("demand")
    if f["soc_min"] <= f["reserve"] + 5:
        k.append("battery")
    if f["ev_held"] >= 1:
        k.append("ev")
    if f["twin_pct"] < 80:
        k.append("twin")
    return k[:3] or ["none"]


def replay_phrases(f):
    d = f["kept_cost"] - f["plan_cost"]
    p = {"strategy": f["strategy"], "run": f"{f['start']} to {f['end']}", "intervals_played": f"{f['steps']:.0f} intervals of 15 minutes",
         "original_plan_cost": _money(f["plan_cost"]), "cost_if_old_plan_kept": _money(f["kept_cost"]),
         "cost_after_replanning": _money(f["new_cost"]), "solar_vs_forecast": f"{f['solar_pct']:.0f}%", "demand_vs_forecast": f"{f['demand_pct']:.0f}%",
         "billed_peak_planned": f"{f['plan_peak']:,.0f} kW", "billed_peak_now": f"{f['new_peak']:,.0f} kW",
         "digital_twin_within_tolerance": f"{f['twin_pct']:.0f}% of intervals"}
    p["day_moved_from_plan"] = (("+" if d >= 0 else "-") + _money(abs(d)))
    r = f["kept_cost"] - f["new_cost"]
    p["recovered_by_replanning"] = _money(max(r, 0))
    if f["ev_held"]:
        p["ev_intervals_held"] = f"{f['ev_held']:.0f} intervals"
    p["improve"] = [IMPROVE[k] for k in improve_keys(f)]
    return p


def replay_template(f):
    p = replay_phrases(f)
    s = [f"In this simulated run ({p['run']}, {p['strategy']}), the day moved {p['day_moved_from_plan']} from the original plan of "
         f"{p['original_plan_cost']}, with solar at {p['solar_vs_forecast']} and demand at {p['demand_vs_forecast']} of forecast.",
         f"Re-planning every 15 minutes recovered {p['recovered_by_replanning']}, so the day now costs {p['cost_after_replanning']}.",
         f"The billed peak was planned at {p['billed_peak_planned']} and is now {p['billed_peak_now']}."]
    s.append("To improve: " + "; ".join(p["improve"]) + ".")
    return " ".join(s)


def explain_replay(raw, model=None):
    f = validate_replay(raw)
    p = replay_phrases(f)
    base = {"text": replay_template(f), "source": "template", "note": "", "improve": improve_keys(f)}
    try:
        text = " ".join((model or call_replay)(p).split())
    except Unavailable as e:
        base["note"] = str(e)
        return base
    except Exception as e:
        logging.warning("replay review failed: %s", e)
        base["note"] = "AI model error"
        return base
    flat = {k: (" ".join(v) if isinstance(v, list) else v) for k, v in p.items()}
    if not text or len(text) > 1100 or "http" in text.lower() or "```" in text or bad_numbers(text, flat):
        base["note"] = "AI answer was rejected: it used numbers that are not in the run"
        return base
    return {"text": text, "source": "ai", "note": "", "improve": base["improve"]}


def call_replay(p):
    return call_model(p, system=REPLAY_SYSTEM)
