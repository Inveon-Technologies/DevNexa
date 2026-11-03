"""Live user traffic for apps deployed through DevNexa.

Every request to https://<app>.<apps_domain> passes through Caddy, which writes one JSON line per request to an
access log (see infra/caddy/Caddyfile). A background thread tails that file and keeps the last hour in memory as
per-minute buckets, plus a short list of the most recent requests. The dashboard reads it through /api/traffic.

In simulated mode (no Caddy) a generator produces plausible traffic so the dashboard can be demonstrated; the API
marks that data as simulated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field

from ..config import get_settings

log = logging.getLogger(__name__)

BUCKET_S = 60
KEEP_MINUTES = 60
RECENT_MAX = 500
LATENCY_SAMPLES = 400  # per bucket, enough for a stable p95


@dataclass
class Hit:
    ts: float
    app: str
    method: str
    path: str
    status: int
    ms: float
    visitor: str
    client: str
    ua: str = ""


@dataclass
class _AppAgg:
    requests: int = 0
    errors: int = 0
    client_errors: int = 0
    latencies: list = field(default_factory=list)
    visitors: set = field(default_factory=set)


@dataclass
class _Bucket:
    minute: int
    requests: int = 0
    errors: int = 0  # 5xx
    client_errors: int = 0  # 4xx
    latencies: list = field(default_factory=list)
    visitors: set = field(default_factory=set)
    apps: dict = field(default_factory=dict)
    problems: Counter = field(default_factory=Counter)  # (app, method, path, status) -> count
    last_seen: dict = field(default_factory=dict)  # same key -> ts


def _p95(values: list) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(len(v) * 0.95))], 1)


def _sample(lst: list, value: float) -> None:
    if len(lst) < LATENCY_SAMPLES:
        lst.append(value)
    else:  # reservoir sampling keeps the percentile honest under heavy traffic
        j = random.randrange(LATENCY_SAMPLES * 4)
        if j < LATENCY_SAMPLES:
            lst[j] = value


def mask_ip(ip: str) -> str:
    if not ip:
        return "unknown"
    if ":" in ip:  # IPv6: keep the first three groups
        return ":".join(ip.split(":")[:3]) + ":…"
    parts = ip.split(".")
    return ".".join(parts[:3] + ["x"]) if len(parts) == 4 else ip


class TrafficStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[int, _Bucket] = {}
        self._recent: deque[Hit] = deque(maxlen=RECENT_MAX)
        self.source = "none"
        self.note = ""
        self.started_at = time.time()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._recent.clear()

    # ----------------------------------------------------------- ingest
    def add(self, hit: Hit) -> None:
        minute = int(hit.ts // BUCKET_S)
        with self._lock:
            b = self._buckets.get(minute)
            if b is None:
                b = self._buckets[minute] = _Bucket(minute)
                floor = minute - KEEP_MINUTES
                for m in [m for m in self._buckets if m < floor]:
                    del self._buckets[m]
            b.requests += 1
            _sample(b.latencies, hit.ms)
            b.visitors.add(hit.visitor)
            a = b.apps.get(hit.app)
            if a is None:
                a = b.apps[hit.app] = _AppAgg()
            a.requests += 1
            _sample(a.latencies, hit.ms)
            a.visitors.add(hit.visitor)
            if hit.status >= 500:
                b.errors += 1
                a.errors += 1
            elif hit.status >= 400:
                b.client_errors += 1
                a.client_errors += 1
            if hit.status >= 400:
                key = (hit.app, hit.method, hit.path, hit.status)
                b.problems[key] += 1
                b.last_seen[key] = max(b.last_seen.get(key, 0), hit.ts)
            self._recent.append(hit)

    # ------------------------------------------------------------ query
    def snapshot(self, minutes: int = 30, app: str | None = None, errors_only: bool = False,
                 recent_limit: int = 50, now: float | None = None, path: str | None = None,
                 allowed: set[str] | None = None) -> dict:
        """`allowed`: only count these apps (a member's own); None counts every app."""
        now = now if now is not None else time.time()
        minutes = max(1, min(KEEP_MINUTES, minutes))
        cur = int(now // BUCKET_S)
        with self._lock:
            buckets = [self._buckets.get(m) for m in range(cur - minutes + 1, cur + 1)]
            recent = list(self._recent)

        def keep(name: str) -> bool:
            return (not app or name == app) and (allowed is None or name in allowed)

        def view(b: _Bucket | None):
            """The part of a bucket that belongs to the selected app (or all of it)."""
            if b is None:
                return None
            if not app and allowed is None:
                return b.requests, b.errors, b.client_errors, b.latencies, b.visitors
            parts = [a for name, a in b.apps.items() if keep(name)]
            if not parts:
                return None
            return (sum(a.requests for a in parts), sum(a.errors for a in parts),
                    sum(a.client_errors for a in parts), [x for a in parts for x in a.latencies],
                    set().union(*[a.visitors for a in parts]))

        series = []
        for i, b in enumerate(buckets):
            v = view(b)
            minute = cur - minutes + 1 + i
            req, err, cerr, lat, _ = v if v else (0, 0, 0, [], set())
            series.append({"t": minute * BUCKET_S, "requests": req, "errors": err, "client_errors": cerr,
                           "p95_ms": _p95(lat)})

        # Headline numbers use the last 5 minutes so they react quickly.
        last5 = [view(b) for b in buckets[-5:]]
        last5 = [v for v in last5 if v]
        req5 = sum(v[0] for v in last5)
        err5 = sum(v[1] for v in last5)
        vis5: set = set().union(*[v[4] for v in last5]) if last5 else set()
        lat5 = [x for v in last5 for x in v[3]]
        span5 = 4 + (now % BUCKET_S) / BUCKET_S  # four full minutes plus the part of this one that has passed
        win = [view(b) for b in buckets]
        win = [v for v in win if v]
        visitors_win: set = set().union(*[v[4] for v in win]) if win else set()

        apps: dict[str, dict] = {}
        problems: Counter = Counter()
        last_seen: dict = {}
        for b in buckets:
            if b is None:
                continue
            for name, a in b.apps.items():
                if not keep(name):
                    continue
                row = apps.setdefault(name, {"app": name, "requests": 0, "errors": 0, "client_errors": 0,
                                             "_lat": [], "_vis": set(), "last_seen": None})
                row["requests"] += a.requests
                row["errors"] += a.errors
                row["client_errors"] += a.client_errors
                row["_lat"].extend(a.latencies)
                row["_vis"] |= a.visitors
            for key, n in b.problems.items():
                if not keep(key[0]):
                    continue
                problems[key] += n
                last_seen[key] = max(last_seen.get(key, 0), b.last_seen[key])
        for h in recent:
            if h.app in apps:
                apps[h.app]["last_seen"] = h.ts
        app_rows = []
        for row in apps.values():
            lat = row.pop("_lat")
            vis = row.pop("_vis")
            row["p95_ms"] = _p95(lat)
            row["visitors"] = len(vis)
            row["error_rate"] = round(row["errors"] / row["requests"], 4) if row["requests"] else 0
            app_rows.append(row)
        app_rows.sort(key=lambda r: -r["requests"])

        problem_rows = [{"app": k[0], "method": k[1], "path": k[2], "status": k[3], "count": n,
                         "last_seen": last_seen[k], "severity": "error" if k[3] >= 500 else "warning"}
                        for k, n in problems.items()]
        problem_rows.sort(key=lambda r: (r["severity"] != "error", -r["count"]))

        floor = (cur - minutes + 1) * BUCKET_S
        feed = [h for h in reversed(recent) if h.ts >= floor and keep(h.app)
                and (not errors_only or h.status >= 400) and (not path or h.path == path)][:recent_limit]
        return {
            "source": self.source,
            "simulated": self.source == "simulated",
            "note": self.note,
            "now": now,
            "minutes": minutes,
            "summary": {
                "requests_per_min": round(req5 / span5, 1),
                "visitors_5m": len(vis5),
                "visitors": len(visitors_win),
                "error_rate_5m": round(err5 / req5, 4) if req5 else 0,
                "errors_5m": err5,
                "p95_ms_5m": _p95(lat5),
                "requests": sum(s["requests"] for s in series),
                "errors": sum(s["errors"] for s in series),
                "client_errors": sum(s["client_errors"] for s in series),
            },
            "series": series,
            "apps": app_rows,
            "problems": problem_rows[:20],
            "recent": [{"ts": h.ts, "app": h.app, "method": h.method, "path": h.path, "status": h.status,
                        "ms": round(h.ms, 1), "client": h.client, "ua": h.ua[:120]} for h in feed],
        }


store = TrafficStore()


# ------------------------------------------------------------ Caddy log
def app_from_host(host: str, domain: str) -> str | None:
    host = (host or "").split(":")[0].lower()
    if not domain or not host.endswith("." + domain):
        return None
    sub = host[: -len(domain) - 1]
    return sub if sub and "." not in sub else None


def parse_caddy_line(line: str, domain: str) -> Hit | None:
    """Turn one Caddy JSON access-log line into a Hit, or None for lines that are not app requests."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    req = e.get("request") or {}
    if not isinstance(req, dict) or "status" not in e:
        return None
    host = req.get("host", "")
    app = app_from_host(host, domain) if domain else (host.split(".")[0] if host else None)
    if not app:
        return None
    ip = req.get("client_ip") or req.get("remote_ip") or ""
    headers = req.get("headers") or {}
    ua = (headers.get("User-Agent") or [""])[0] if isinstance(headers, dict) else ""
    uri = req.get("uri") or "/"
    path = uri.split("?", 1)[0] or "/"
    visitor = hashlib.sha1(f"{ip}|{ua}".encode()).hexdigest()[:12]
    try:
        return Hit(ts=float(e.get("ts") or time.time()), app=app, method=req.get("method", "GET"), path=path[:200],
                   status=int(e.get("status") or 0), ms=float(e.get("duration") or 0) * 1000, visitor=visitor,
                   client=mask_ip(ip), ua=ua)
    except (TypeError, ValueError):
        return None


def _tail(path: str, domain: str) -> None:
    """Follow the access log like `tail -F`, surviving Caddy's log rotation."""
    fh = None
    inode = None
    pending = b""
    while True:
        try:
            if fh is None:
                if not os.path.exists(path):
                    store.note = f"Waiting for {path}. Start Caddy with `--profile https` to record traffic."
                    time.sleep(5)
                    continue
                fh = open(path, "rb")
                inode = os.fstat(fh.fileno()).st_ino
                pending = b""
                # Backfill the last hour from the end of the file so the chart is not empty after a restart.
                size = os.fstat(fh.fileno()).st_size
                if size > 4_000_000:
                    fh.seek(size - 4_000_000)
                    fh.readline()
                floor = time.time() - KEEP_MINUTES * BUCKET_S
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        pending = raw
                        break
                    hit = parse_caddy_line(raw.decode("utf-8", "replace"), domain)
                    if hit and hit.ts >= floor:
                        store.add(hit)
                store.note = ""
            raw = fh.readline()
            if raw:
                pending += raw
                if pending.endswith(b"\n"):
                    hit = parse_caddy_line(pending.decode("utf-8", "replace"), domain)
                    pending = b""
                    if hit:
                        store.add(hit)
                continue
            time.sleep(0.5)
            try:
                st = os.stat(path)
                if st.st_ino != inode or st.st_size < fh.tell():
                    fh.close()
                    fh = None
            except FileNotFoundError:
                fh.close()
                fh = None
        except Exception:  # pragma: no cover - keep the tailer alive
            log.exception("traffic tailer error")
            if fh:
                fh.close()
            fh = None
            time.sleep(5)


# ---------------------------------------------------------- simulation
SIM_APPS = {
    "shop-frontend": ["/", "/products", "/products/42", "/cart", "/checkout", "/login", "/static/app.js"],
    "orders-api": ["/api/orders", "/api/orders/1187", "/api/health", "/api/users/me"],
}


def _sim_rate(ts: float) -> float:
    """Requests per second: a slow wave plus the odd burst, so the chart looks like real visitors."""
    import math

    base = 0.6 + 0.35 * math.sin(ts / 420) + 0.2 * math.sin(ts / 97)
    return max(0.15, base + (1.2 if int(ts / 60) % 13 == 0 else 0))


def _simulate() -> None:
    rnd = random.Random()
    visitors = [(f"198.51.100.{i}", f"v{i}") for i in range(1, 140)]
    t0 = time.time()
    # Backfill 30 minutes so the chart has shape immediately.
    for s in range(30 * 60, 0, -1):
        ts = t0 - s
        for _ in range(_poisson(rnd, _sim_rate(ts))):
            _sim_hit(rnd, visitors, ts + rnd.random(), t0)
    while True:
        now = time.time()
        for _ in range(_poisson(rnd, _sim_rate(now))):
            _sim_hit(rnd, visitors, now, t0)
        time.sleep(1.0)


def _poisson(rnd: random.Random, lam: float) -> int:
    n, p, limit = 0, 1.0, 2.718281828 ** -lam
    while True:
        p *= rnd.random()
        if p <= limit:
            return n
        n += 1


def _sim_hit(rnd: random.Random, visitors, ts: float, t0: float) -> None:
    app = rnd.choice(list(SIM_APPS))
    path = rnd.choice(SIM_APPS[app])
    ip, vid = rnd.choice(visitors)
    status, ms = 200, rnd.lognormvariate(3.6, 0.5)
    # A recurring checkout bug every ~10 minutes, and the odd broken link.
    if path == "/checkout" and int(ts / 60) % 10 in (3, 4, 5) and rnd.random() < 0.6:
        status, ms = 500, ms * 3
    elif path == "/products/42" and rnd.random() < 0.15:
        status = 404
    elif path == "/api/users/me" and rnd.random() < 0.05:
        status = 401
    store.add(Hit(ts=ts, app=app, method="POST" if path in ("/checkout", "/api/orders") and rnd.random() < .5 else "GET",
                  path=path, status=status, ms=ms, visitor=vid, client=mask_ip(ip), ua="Mozilla/5.0 (simulated)"))


_started = False


def start() -> None:
    """Start the traffic source for this mode: tail Caddy's log in live mode, simulate otherwise."""
    global _started
    if _started:
        return
    _started = True
    s = get_settings()
    if s.traffic_source == "off":
        return
    if s.traffic_source == "caddy" or (s.traffic_source == "auto" and s.is_live):
        store.source = "caddy"
        if not s.apps_domain:
            store.note = "Set DEVNEXA_APPS_DOMAIN so app traffic goes through Caddy and can be recorded."
        threading.Thread(target=_tail, args=(s.traffic_log, s.apps_domain), name="devnexa-traffic",
                         daemon=True).start()
    else:
        store.source = "simulated"
        store.note = "Simulated traffic: run in live mode behind Caddy to see real visitors."
        threading.Thread(target=_simulate, name="devnexa-traffic-sim", daemon=True).start()
