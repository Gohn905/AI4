"""Checks for ratelimit.py. Run:  python test_ratelimit.py"""
import ratelimit as rl

res = []
ok = lambda n, c: print(("PASS  " if c else "FAIL  ") + n) or res.append(bool(c))

ok("first calls up to the limit are allowed", all(rl.allow("a", 3, 60, now=t)[0] for t in (0, 1, 2)))
a, wait = rl.allow("a", 3, 60, now=3)
ok("the next call is refused with a wait time", (not a) and 1 <= wait <= 60)
ok("another client is not affected", rl.allow("b", 3, 60, now=3)[0])
ok("allowed again after the window passes", rl.allow("a", 3, 60, now=61)[0])
rl.allow("d", 2, 60, now=0); rl.allow("d", 2, 60, now=1)
refused = [rl.allow("d", 2, 60, now=t)[0] for t in (30, 31, 32)]
ok("a full window refuses further calls", refused == [False, False, False])
ok("refused calls are not counted: when the old hits expire the client is allowed again", rl.allow("d", 2, 60, now=60)[0] and rl.allow("d", 2, 60, now=61)[0] and not rl.allow("d", 2, 60, now=61.5)[0])
ok("client key uses the first forwarded address", rl.client_key({"x-forwarded-for": "1.2.3.4, 10.0.0.1"}, "r") == "r|1.2.3.4")
ok("client key falls back when the header is missing", rl.client_key({}, "r") == "r|unknown")
for i in range(rl.MAX_KEYS + 50):
    rl.allow(f"k{i}", 1, 60, now=100 + i)
ok("table size stays bounded", len(rl._hits) <= rl.MAX_KEYS)
print(f"\n{sum(res)}/{len(res)} passed"); raise SystemExit(0 if all(res) else 1)
