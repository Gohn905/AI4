"""Saved facility profiles: a private set per signed-in user, in Azure Table Storage.

Privacy design
- The row owner is the pseudonymous user id derived from the sign-in header (see audit.caller),
  so one user can never read or overwrite another user's rows.
- Only whitelisted, range-checked settings are stored, plus a 24-value typical-day profile.
  The raw meter file is never uploaded or stored.
- Up to 5 sites per user, 10 KB each. A user can delete one site or everything.
- Azure Storage encrypts data at rest. Uses the same storage account as the audit log.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

import audit

TABLE = "profiles"
MAX_SITES, MAX_BYTES = 5, 10000
_state = {"table": None}

NUM = {"peak": (1, 1e5), "kwp": (0, 1e5), "bat": (0, 20000), "flex": (0, 30), "afa": (-20, 20), "ef": (0, 2), "lim": (0, 1e6)}
CHOICE = {"volt": ("LV", "MV", "HV"), "struct": ("G", "T"), "prof": ("campus", "factory"),
          "follow": ("base", "cost", "bal", "prod"), "loc": ("3.0553,101.7003", "5.41,100.33")}
BOOL = ()


class Unavailable(Exception):
    pass


class NotFound(Exception):
    pass


def clean(d):
    """Keep only known fields, with types and ranges checked. Anything else is dropped."""
    if not isinstance(d, dict):
        raise ValueError("data must be an object")
    out = {}
    for k, (lo, hi) in NUM.items():
        if d.get(k) in (None, ""):
            continue
        try:
            x = float(d[k])
        except (TypeError, ValueError):
            raise ValueError(k + " must be a number")
        if not lo <= x <= hi:                      # also rejects NaN and infinity
            raise ValueError(f"{k} must be between {lo} and {hi}")
        out[k] = x
    for k, opts in CHOICE.items():
        if k in d:
            if d[k] not in opts:
                raise ValueError(k + " has an unknown value")
            out[k] = d[k]
    for k in BOOL:
        if k in d:
            out[k] = bool(d[k])
    m = d.get("meter")
    if m is not None:
        if not (isinstance(m, list) and len(m) == 24):
            raise ValueError("meter must be 24 numbers")
        m = [float(v) for v in m]
        if any(not 0 <= v <= 1e6 for v in m):
            raise ValueError("meter values out of range")
        out["meter"] = m
    if out.get("prof") == "meter" and "meter" not in out:
        raise ValueError("meter profile is missing")
    return out


def _name(n):
    n = (n or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9 _.\-]{1,40}", n):
        raise ValueError("name: 1 to 40 letters, numbers, spaces, dot, dash or underscore")
    return n, re.sub(r"\s+", "_", n.lower())


def _table():
    if _state["table"] is None:
        if not audit.configured():
            raise Unavailable("storage is not connected (app setting AUDIT_STORAGE_CONNECTION or AUDIT_STORAGE_ACCOUNT_URL)")
        try:
            _state["table"] = audit.open_table(TABLE)
        except Exception as e:
            logging.warning("profile storage unavailable: %s", e)
            raise Unavailable("storage unavailable (%s: %s)" % (type(e).__name__, str(e).splitlines()[0][:160] if str(e) else ""))
    return _state["table"]


def uid_of(req):
    u = audit.caller(req)
    return None if u in ("anonymous", "unknown") else u


def list_sites(u):
    rows = _table().query_entities("PartitionKey eq @u", parameters={"u": u}, select=["name", "updated"])
    return sorted(({"name": r["name"], "updated": r["updated"]} for r in rows), key=lambda r: r["name"].lower())


def get_site(u, name):
    _, key = _name(name)
    try:
        return json.loads(_table().get_entity(partition_key=u, row_key=key)["data"])
    except Unavailable:
        raise
    except Exception:
        raise NotFound("site not found")


def put_site(u, name, data):
    name, key = _name(name)
    blob = json.dumps(clean(data), separators=(",", ":"))
    if len(blob) > MAX_BYTES:
        raise ValueError("profile too large")
    t = _table()
    existing = {s["name"].lower() for s in list_sites(u)}
    if name.lower() not in existing and len(existing) >= MAX_SITES:
        raise ValueError(f"limit of {MAX_SITES} saved sites reached, delete one first")
    t.upsert_entity({"PartitionKey": u, "RowKey": key, "name": name, "data": blob,
                     "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})


def delete_site(u, name):
    _, key = _name(name)
    _table().delete_entity(partition_key=u, row_key=key)


def delete_all(u):
    n = 0
    for s in list_sites(u):
        _table().delete_entity(partition_key=u, row_key=_name(s["name"])[1])
        n += 1
    return n
