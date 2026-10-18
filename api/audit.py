"""Audit log: one row per optimise / forecast call, kept in Azure Table Storage.

Why: every recommendation can be traced later (who asked, what went in, what came out, how long).
Storage access, either way:
  AUDIT_STORAGE_CONNECTION = full connection string (secret), or
  AUDIT_STORAGE_ACCOUNT_URL = https://<account>.table.core.windows.net  (managed identity, no secret;
  the Function App identity needs the role "Storage Table Data Contributor" on the storage account).
Without either one the API still works and only
writes a structured log line (which Application Insights collects when it is switched on).
Logging must never break a request, so every storage call is wrapped.
"""
import base64
import hashlib
import json
import logging
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

TABLE = "audit"
RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "30"))
_state = {"table": None, "failed": False}


def configured():
    return bool(os.environ.get("AUDIT_STORAGE_CONNECTION") or os.environ.get("AUDIT_STORAGE_ACCOUNT_URL"))


def enabled():
    return configured() and not _state["failed"]


def open_table(name):
    """Table client for `name`, or None if no storage is configured. Raises if the storage cannot be used."""
    cs = os.environ.get("AUDIT_STORAGE_CONNECTION")
    url = os.environ.get("AUDIT_STORAGE_ACCOUNT_URL")
    if not cs and not url:
        return None
    from azure.data.tables import TableServiceClient
    if cs:
        svc = TableServiceClient.from_connection_string(cs)
    else:
        from azure.identity import DefaultAzureCredential
        svc = TableServiceClient(endpoint=url, credential=DefaultAzureCredential())
    try:
        return svc.create_table_if_not_exists(name)
    except Exception as e:
        if cs:
            raise
        logging.info("could not create table %s (%s); using it if it already exists", name, type(e).__name__)
        return svc.get_table_client(name)


def _table():
    if _state["table"] is not None:
        return _state["table"]
    if not configured() or _state["failed"]:
        return None
    try:
        _state["table"] = open_table(TABLE)
        return _state["table"]
    except Exception as e:  # never let logging break the API
        logging.warning("audit storage disabled: %s", e)
        _state["failed"] = True
        return None


def caller(req):
    """Opaque user id from the Static Web Apps sign-in header (no email stored)."""
    raw = req.headers.get("x-ms-client-principal")
    if not raw:
        return "anonymous"
    try:
        uid = json.loads(base64.b64decode(raw)).get("userId")
        if not uid:
            return "unknown"
        salt = os.environ.get("AUDIT_SALT", "facility-planner")      # one-way: the platform user id is not stored
        return hashlib.sha256((salt + ":" + str(uid)).encode()).hexdigest()[:16]
    except Exception:
        return "unknown"


def _private(obj):
    """Coarsen anything that locates a customer site before it is stored (0.1 degree, about 11 km)."""
    if isinstance(obj, dict):
        o = dict(obj)
        for k in ("lat", "lon"):
            if k in o:
                try:
                    o[k] = round(float(o[k]), 1)
                except (TypeError, ValueError):
                    o[k] = None
        return o
    return obj


def purge(days=None):
    """Delete audit rows older than the retention period (default 30 days). Never raises."""
    table = _table()
    if table is None:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days or RETENTION_DAYS)).strftime("%Y-%m-%d")
    n = 0
    try:
        for e in table.query_entities("PartitionKey lt @c", parameters={"c": cutoff}, select=["PartitionKey", "RowKey"]):
            table.delete_entity(partition_key=e["PartitionKey"], row_key=e["RowKey"])
            n += 1
            if n >= 200:
                break
    except Exception as ex:
        logging.warning("audit purge failed: %s", ex)
    return n


def _clip(obj, n=30000):
    s = json.dumps(obj, separators=(",", ":"), default=str) if obj is not None else ""
    return s[:n]


def record(route, req, status, ms, request=None, result=None, model="", summary=""):
    who = caller(req)
    logging.info("audit %s", json.dumps({"route": route, "status": status, "ms": int(ms), "user": who,
                                         "model": model, "summary": summary}))
    table = _table()
    if table is None:
        return
    now = datetime.now(timezone.utc)
    try:
        table.create_entity({
            "PartitionKey": now.strftime("%Y-%m-%d"),
            "RowKey": now.strftime("%H%M%S%f") + "-" + uuid.uuid4().hex[:6],
            "route": route, "status": int(status), "ms": int(ms), "user": who, "model": model or "",
            "summary": summary[:500], "request": _clip(_private(request)), "result": _clip(result),
        })
    except Exception as e:
        logging.warning("audit write failed: %s", e)
    if random.random() < 0.02:          # housekeeping: now and then, remove rows past the retention period
        purge()


def recent(limit=15):
    table = _table()
    if table is None:
        return []
    now = datetime.now(timezone.utc)
    rows = []
    try:
        for day in ((now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(2)):
            for n, e in enumerate(table.query_entities("PartitionKey eq @d", parameters={"d": day})):
                if n >= 2000:
                    break
                rows.append(e)
    except Exception as e:
        logging.warning("audit read failed: %s", e)
        return []
    rows.sort(key=lambda e: (e["PartitionKey"], e["RowKey"]), reverse=True)
    return [{"time": e["PartitionKey"] + " " + e["RowKey"][:2] + ":" + e["RowKey"][2:4] + ":" + e["RowKey"][4:6] + " UTC",
             "route": e.get("route"), "user": str(e.get("user", ""))[:8], "status": e.get("status"),
             "ms": e.get("ms"), "model": e.get("model"), "summary": e.get("summary")} for e in rows[:limit]]
