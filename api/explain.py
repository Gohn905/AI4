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
from collections import deque
from urllib.error import HTTPError
from urllib.request import Request, urlopen

STRATS = ("Baseline", "Cost-saving", "Balanced", "No extra CO2", "Production-first")
TARIFFS = ("LV General", "LV Time-of-Use", "MV General", "MV Time-of-Use", "HV General", "HV Time-of-Use")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
RUN = re.compile(r"^\d{2}:00 to \d{2}:00(, \d{2}:00 to \d{2}:00){0,5}$")
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_calls = deque()
MAX_PER_MIN = 8            # stay under the deployment's 10 requests per minute

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
            raise ValueError(k + " must look like 10:00 to 13:00")
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
    ep = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
    for tail in ("/openai/v1", "/openai"):
        if ep.endswith(tail):
            ep = ep[: -len(tail)]
    return ep


def call_model(p, fix=None):
    ep, dep = _endpoint(), os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
    try:
        auth = aoai.auth_headers()
    except Exception as e:
        raise Unavailable("AI sign-in failed: " + type(e).__name__)
    if not (ep and dep and auth):
        raise Unavailable("AI model is not configured")
    now = time.time()
    while _calls and now - _calls[0] > 60:
        _calls.popleft()
    if len(_calls) >= MAX_PER_MIN:
        raise Unavailable("AI model is busy")
    _calls.append(now)
    body = {"model": dep, "max_completion_tokens": 900,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(p)}]
            + ([{"role": "assistant", "content": fix["text"]}, {"role": "user", "content": fix["msg"]}] if fix else [])}
    eff = os.environ.get("AZURE_OPENAI_REASONING", "minimal")
    if eff:
        body["reasoning_effort"] = eff
    url = ep + "/openai/v1/chat/completions"
    last = ""
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
            last = _detail(e, raw)
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
