"""Small in-process sliding-window rate limiter. One instance of the server only: with several instances each keeps its own
count, so this limits accidents and casual abuse, not a determined attacker. Add a shared limit (API Management, Front Door)
before production use."""
import time
from collections import deque

_hits = {}                 # key -> deque of timestamps
MAX_KEYS = 2000            # bound the memory used by the table


def allow(key, limit, per_s=60.0, now=None):
    """True if this call is allowed. Returns (allowed, retry_after_seconds)."""
    now = time.monotonic() if now is None else now
    q = _hits.get(key)
    if q is None:
        if len(_hits) >= MAX_KEYS:                       # drop the oldest-idle keys instead of growing forever
            for k in sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0)[: MAX_KEYS // 4]:
                del _hits[k]
        q = _hits[key] = deque()
    while q and now - q[0] >= per_s:
        q.popleft()
    if len(q) >= limit:
        return False, max(1, int(per_s - (now - q[0])) + 1)
    q.append(now)
    return True, 0


def client_key(headers, route):
    """Client id from the proxy header (Azure sets x-forwarded-for); falls back to one shared bucket."""
    fwd = (headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return f"{route}|{fwd[:64] or 'unknown'}"
