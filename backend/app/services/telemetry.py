"""Incident evidence collection: logs, metrics, traces, recent commits and deployment history."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Deployment, Incident, PipelineRun
from ..simulation.catalog import BASE_METRICS, FAULTS, SERVICE

NOISE_LOGS = [
    ("INFO", "GET /health 200 2ms"),
    ("INFO", "GET /orders?page=2 200 41ms"),
    ("INFO", "POST /orders 201 88ms"),
    ("DEBUG", "cache hit ratio 0.83"),
    ("INFO", "scheduler: reconciled 12 pending orders"),
    ("WARN", "slow client detected, closing keep-alive connection"),
    ("INFO", "GET /metrics 200 3ms"),
]

METRIC_LABELS = {
    "http_error_rate": "HTTP 5xx error ratio",
    "http_p95_latency_ms": "p95 request latency (ms)",
    "cpu_usage": "CPU usage",
    "memory_mb": "Resident memory (MB)",
    "db_connections_in_use": "DB connections in use",
    "upstream_error_rate": "Upstream error ratio",
    "container_restarts": "Container restarts",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def change_evidence(db: Session, now: datetime) -> list[dict]:
    runs = db.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(5)).all()
    deps = db.scalars(select(Deployment).order_by(Deployment.id.desc()).limit(5)).all()
    out = [{
        "kind": "commit", "source": "devnexa", "title": f"Recent pipeline commits ({len(runs)})",
        "content": {"commits": [{
            "sha": r.commit_sha[:7], "message": r.commit_message, "author": r.author,
            "files": r.changed_files, "branch": r.branch, "time": _iso(r.created_at),
            "pipeline_run": r.id, "status": r.status,
        } for r in runs]},
    }, {
        "kind": "deployment", "source": "devnexa", "title": f"Deployment history ({len(deps)})",
        "content": {"deployments": [{
            "id": d.id, "version": d.version, "image": d.image, "commit_sha": d.commit_sha[:7],
            "status": d.status, "health": d.health, "is_stable": d.is_stable,
            "deployed_at": _iso(d.deployed_at),
            "minutes_before_alert": round((now - d.deployed_at).total_seconds() / 60, 1),
        } for d in deps]},
    }]
    return out


def simulated_evidence(db: Session, incident: Incident, faults: dict, rng: random.Random,
                       observability_gap: float = 0.0, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    specs = [FAULTS[f] for f in faults if f in FAULTS]

    # ---- metrics: 30 samples over 15 minutes, fault onset ~5 minutes ago
    onset = 20
    series = []
    for i in range(30):
        t = now - timedelta(seconds=30 * (29 - i))
        point = {"t": _iso(t)}
        for name, base in BASE_METRICS.items():
            v = base * (1 + rng.uniform(-0.08, 0.08))
            for s in specs:
                if name in s.metrics and i >= onset:
                    ramp = min(1.0, (i - onset + 1) / 4)
                    v = max(v, s.metrics[name][0] + (s.metrics[name][1] - s.metrics[name][0]) * ramp
                            * (1 + rng.uniform(-0.05, 0.05)))
            point[name] = round(v, 4)
        series.append(point)
    anomalies = []
    # Secondary metrics are not always exported/scraped for the affected path.
    primary = {next(iter(s.metrics)) for s in specs}
    for name, base in BASE_METRICS.items():
        cur = series[-1][name]
        if name not in primary and rng.random() < observability_gap / 2:
            continue
        if abs(cur - base) > max(0.05 * abs(base), 0.01) and any(name in s.metrics for s in specs):
            anomalies.append({"metric": name, "label": METRIC_LABELS[name], "baseline": base,
                              "current": cur, "direction": "up" if cur > base else "down"})

    # ---- logs: distinctive lines may be missing (observability gaps), generic ones always present
    lines = []
    version = (faults.get(specs[0].id, {}) if specs else {}).get("version", "1.4.x")
    for s in specs:
        for msg in s.distinctive_logs:
            if rng.random() >= observability_gap:
                lines.append(("ERROR", msg.replace("{version}", str(version).lstrip("v"))))
        for msg in s.generic_logs:
            lines.append(("ERROR" if "HTTP 5" in msg or "failed" in msg else "WARN", msg))
    for _ in range(6):
        lines.append(rng.choice(NOISE_LOGS))
    rng.shuffle(lines)
    log_entries = [{"ts": _iso(now - timedelta(seconds=rng.randint(5, 290))), "level": lvl, "message": m,
                    "service": SERVICE} for lvl, m in lines]
    log_entries.sort(key=lambda e: e["ts"])

    # ---- traces
    traces = []
    for s in specs:
        if rng.random() < observability_gap:  # not every request path is instrumented
            continue
        tr = s.trace
        spans = [{"name": tr["root"], "duration_ms": tr["duration_ms"], "error": bool(tr["error_span"]),
                  "depth": 0}]
        for child in ["auth.verify_token", "cache.get", "db.query orders", "payments.charge"]:
            dur = rng.randint(2, 30)
            err = False
            if tr["slow_span"] and child.split()[0] == tr["slow_span"].split()[0]:
                dur = int(tr["duration_ms"] * 0.9)
            if tr["error_span"] and child.split(".")[0] == tr["error_span"].split(".")[0]:
                err = True
            spans.append({"name": child, "duration_ms": dur, "error": err, "depth": 1})
        if tr["error_span"] and not any(sp["error"] for sp in spans[1:]):
            spans.append({"name": tr["error_span"], "duration_ms": rng.randint(1, 10), "error": True, "depth": 1})
        if tr["slow_span"] and not any(sp["name"] == tr["slow_span"] for sp in spans):
            spans.append({"name": tr["slow_span"], "duration_ms": int(tr["duration_ms"] * 0.9),
                          "error": False, "depth": 1})
        traces.append({"trace_id": uuid.UUID(int=rng.getrandbits(128)).hex[:16], "root": tr["root"],
                       "duration_ms": tr["duration_ms"], "spans": spans})

    evidence = [
        {"kind": "metric", "source": "prometheus", "title": f"Metrics, last 15 min ({len(anomalies)} anomalous)",
         "content": {"series": series, "anomalies": anomalies, "labels": METRIC_LABELS}},
        {"kind": "log", "source": "loki", "title": f"Logs for {SERVICE} ({len(log_entries)} lines)",
         "content": {"query": f'{{service="{SERVICE}"}} |~ "(?i)error|warn|exception|timeout"',
                     "lines": log_entries}},
        {"kind": "trace", "source": "jaeger", "title": f"Traces ({len(traces)} sampled)",
         "content": {"traces": traces}},
    ]
    return evidence + change_evidence(db, now)


def live_evidence(db: Session, incident: Incident) -> list[dict]:  # pragma: no cover - needs stack
    s = get_settings()
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=15)
    client = httpx.Client(timeout=6.0)
    evidence: list[dict] = []

    queries = {
        "http_error_rate": 'sum(rate(http_requests_total{status=~"5.."}[1m])) / '
                           "clamp_min(sum(rate(http_requests_total[1m])), 1e-9)",
        "http_p95_latency_ms": "histogram_quantile(0.95, sum by (le) "
                               "(rate(http_request_duration_seconds_bucket[1m]))) * 1000",
        "memory_mb": "max(orders_memory_mb)",
        "upstream_error_rate": "max(orders_upstream_error_ratio)",
        "db_connections_in_use": "max(orders_db_connections_in_use)",
    }
    try:
        merged: dict[float, dict] = {}
        for name, q in queries.items():
            r = client.get(f"{s.prometheus_url}/api/v1/query_range",
                           params={"query": q, "start": start.timestamp(), "end": now.timestamp(), "step": 30})
            for res in r.json()["data"]["result"][:1]:
                for ts, val in res["values"]:
                    try:
                        merged.setdefault(ts, {"t": _iso(datetime.fromtimestamp(ts, timezone.utc))})[name] = \
                            round(float(val), 4)
                    except ValueError:
                        pass
        series = [merged[k] for k in sorted(merged)]
        anomalies = []
        if series:
            for name, base in BASE_METRICS.items():
                cur = series[-1].get(name)
                if cur is not None and cur > base * 2 and cur - base > 0.01:
                    anomalies.append({"metric": name, "label": METRIC_LABELS[name], "baseline": base,
                                      "current": cur, "direction": "up"})
        evidence.append({"kind": "metric", "source": "prometheus",
                         "title": f"Metrics, last 15 min ({len(anomalies)} anomalous)",
                         "content": {"series": series, "anomalies": anomalies, "labels": METRIC_LABELS}})
    except Exception as e:
        evidence.append({"kind": "metric", "source": "prometheus", "title": "Prometheus unavailable",
                         "content": {"error": str(e), "series": [], "anomalies": []}})

    try:
        q = '{service="orders-service"} |~ "(?i)error|warn|exception|timeout|slow"'
        r = client.get(f"{s.loki_url}/loki/api/v1/query_range",
                       params={"query": q, "start": int(start.timestamp() * 1e9), "end": int(now.timestamp() * 1e9),
                               "limit": 80})
        lines = []
        for stream in r.json()["data"]["result"]:
            for ts, line in stream["values"]:
                lvl = "ERROR" if "error" in line.lower() or "exception" in line.lower() else "WARN"
                lines.append({"ts": _iso(datetime.fromtimestamp(int(ts) / 1e9, timezone.utc)), "level": lvl,
                              "message": line[:500], "service": SERVICE})
        lines.sort(key=lambda e: e["ts"])
        evidence.append({"kind": "log", "source": "loki", "title": f"Logs for {SERVICE} ({len(lines)} lines)",
                         "content": {"query": q, "lines": lines[-60:]}})
    except Exception as e:
        evidence.append({"kind": "log", "source": "loki", "title": "Loki unavailable",
                         "content": {"error": str(e), "lines": []}})

    try:
        r = client.get(f"{s.jaeger_url}/api/traces",
                       params={"service": SERVICE, "lookback": "15m", "limit": 10, "tags": '{"error":"true"}'})
        traces = []
        for t in r.json().get("data", [])[:5]:
            spans = sorted(t["spans"], key=lambda sp: sp["startTime"])
            root = spans[0]
            traces.append({"trace_id": t["traceID"][:16], "root": root["operationName"],
                           "duration_ms": round(root["duration"] / 1000),
                           "spans": [{"name": sp["operationName"], "duration_ms": round(sp["duration"] / 1000),
                                      "error": any(tg["key"] == "error" and tg["value"] for tg in sp.get("tags", [])),
                                      "depth": 0 if sp is root else 1} for sp in spans[:12]]})
        evidence.append({"kind": "trace", "source": "jaeger", "title": f"Traces ({len(traces)} sampled)",
                         "content": {"traces": traces}})
    except Exception as e:
        evidence.append({"kind": "trace", "source": "jaeger", "title": "Jaeger unavailable",
                         "content": {"error": str(e), "traces": []}})

    return evidence + git_evidence(db) + change_evidence(db, now)


def git_evidence(db: Session) -> list[dict]:  # pragma: no cover - needs GitHub
    """Real commit history of the monitored service's repo (DEVNEXA_GITHUB_REPO), when GitHub is connected."""
    from . import github

    s = get_settings()
    token = github.get_token(db)
    if not (s.github_repo and token):
        return []
    try:
        commits = github.recent_commits(token, s.github_repo)
    except github.GitHubError as e:
        return [{"kind": "commit", "source": "github", "title": "GitHub commits unavailable",
                 "content": {"error": str(e), "commits": []}}]
    prefix = f"{s.sample_app_subdir}/" if s.sample_app_subdir else ""
    for c in commits:  # paths relative to the service, as the analyzer's module map expects
        c["files"] = [f.removeprefix(prefix) for f in c["files"]]
    return [{"kind": "commit", "source": "github", "title": f"Recent commits on {s.github_repo} ({len(commits)})",
             "content": {"commits": commits, "repo": s.github_repo}}]


def symptom_text(incident: Incident, evidence: list[dict]) -> str:
    """Condense evidence into the text that is embedded for similar-incident retrieval."""
    parts = [f"alert {incident.alert_name}", f"service {incident.service}", incident.summary]
    for ev in evidence:
        c = ev["content"]
        if ev["kind"] == "log":
            parts += [ln["message"] for ln in c.get("lines", []) if ln["level"] in ("ERROR", "WARN")]
        elif ev["kind"] == "metric":
            parts += [f"anomaly {a['metric']} {a['direction']}" for a in c.get("anomalies", [])]
        elif ev["kind"] == "trace":
            for t in c.get("traces", []):
                parts.append(f"trace {t['root']}")
                parts += [f"span error {sp['name']}" for sp in t["spans"] if sp.get("error")]
                parts += [f"span slow {sp['name']}" for sp in t["spans"]
                          if sp.get("depth") and sp["duration_ms"] > 300]
    return "\n".join(p for p in parts if p)
