"""How the app signs in to the Azure AI model: an API key, or (better) the app's managed identity.

  AZURE_OPENAI_KEY=<key>            works everywhere (a secret to protect)
  AZURE_OPENAI_USE_IDENTITY=1       no key at all. The Function App's managed identity needs the role
                                    "Cognitive Services OpenAI User" on the AI resource.
If both are set, the key is used. Used by explain.py and assist.py.
"""
import os
import threading
import time
from collections import deque

_tok = {"v": None, "exp": 0.0}

# The summary (explain.py) and the assistant (assist.py) call the SAME model deployment, so they must share ONE per-minute budget.
# (Before, each kept its own counter: 8 + 6 > the deployment's 10 a minute, so one feature could starve the other.)
MAX_PER_MIN = int(os.environ.get("AI_MAX_PER_MIN", "9") or 9)
_recent, _lock = deque(), threading.Lock()


def reserve():
    """Take one slot of this minute's model budget. False = the model is busy, do not call it."""
    now = time.time()
    with _lock:
        while _recent and now - _recent[0] > 60:
            _recent.popleft()
        if len(_recent) >= MAX_PER_MIN:
            return False
        _recent.append(now)
        return True


def retry_wait(err, cap=4.0):
    """Seconds to wait after a 429 from the model: its Retry-After header, capped, default 2 s."""
    try:
        return max(0.5, min(cap, float(err.headers.get("Retry-After", 2))))
    except Exception:
        return 2.0


def configured():
    return bool(os.environ.get("AZURE_OPENAI_KEY") or os.environ.get("AZURE_OPENAI_USE_IDENTITY", "").lower() in ("1", "true", "yes"))


def auth_headers():
    """Headers that prove who we are to the AI endpoint, or None if neither a key nor identity is set up."""
    key = os.environ.get("AZURE_OPENAI_KEY", "")
    if key:
        return {"api-key": key}
    if os.environ.get("AZURE_OPENAI_USE_IDENTITY", "").lower() in ("1", "true", "yes"):
        now = time.time()
        if not _tok["v"] or now > _tok["exp"] - 300:
            from azure.identity import DefaultAzureCredential
            t = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default")
            _tok["v"], _tok["exp"] = t.token, float(t.expires_on)
        return {"Authorization": "Bearer " + _tok["v"]}
    return None
