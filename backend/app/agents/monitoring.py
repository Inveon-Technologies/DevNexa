"""Alert evaluation (simulated mode) and alert ingestion (both modes).

In live mode Prometheus evaluates ``infra/prometheus/alerts.yml`` and Alertmanager posts to
``/api/webhooks/alertmanager``. In simulated mode ``scan`` applies the same rules to the simulated metrics.
"""

from __future__ import annotations

from sqlalchemy import select

from ..models import Alert, Incident
from ..services.audit import audit
from ..simulation.catalog import SERVICE
from .context import RunContext

# name, metric, threshold, severity, summary  (mirrors infra/prometheus/alerts.yml)
ALERT_RULES = [
    ("OrdersServiceDown", "liveness", None, "critical", "orders-service liveness probe failing"),
    ("UpstreamDependencyErrors", "upstream_error_rate", 0.10, "critical", "Upstream call error ratio above 10%"),
    ("OrdersHighErrorRate", "http_error_rate", 0.05, "critical", "HTTP 5xx ratio above 5%"),
    ("OrdersMemoryPressure", "memory_mb", 450.0, "warning", "Resident memory above 450 MB"),
    ("OrdersHighLatency", "http_p95_latency_ms", 1000.0, "warning", "p95 latency above 1s"),
]
OPEN_STATES = ("open", "investigating", "diagnosed", "awaiting_approval", "remediating", "verifying")


def evaluate_rules(env) -> list[dict]:
    m = env.metrics()
    live = env.health()[0]["passed"]
    firing = []
    for name, metric, thr, sev, summary in ALERT_RULES:
        value = (0 if live else 1) if metric == "liveness" else m.get(metric, 0)
        if (metric == "liveness" and not live) or (thr is not None and value >= thr):
            firing.append({"name": name, "severity": sev, "summary": f"{summary} (value {round(value, 3)})",
                           "labels": {"alertname": name, "service": SERVICE, "severity": sev, "metric": metric},
                           "value": value})
    return firing


def ingest_alerts(db, alerts: list[dict], source: str, now, fault_info: dict | None = None) -> Incident | None:
    """Store alerts, group them into one open incident per service. Returns a newly created incident."""
    created = None
    for a in alerts:
        service = a.get("labels", {}).get("service", SERVICE)
        fp = a.get("fingerprint") or f"{a['name']}:{service}"
        if a.get("status", "firing") == "resolved":
            for open_alert in db.scalars(select(Alert).where(Alert.fingerprint == fp, Alert.status == "firing")):
                open_alert.status = "resolved"
            continue
        incident = db.scalars(select(Incident).where(Incident.service == service, Incident.status.in_(OPEN_STATES))
                              .order_by(Incident.id.desc())).first()
        existing = db.scalars(select(Alert).where(Alert.fingerprint == fp, Alert.status == "firing")).first()
        if existing and incident and existing.incident_id == incident.id:
            continue
        if incident is None:
            fault_info = fault_info or {}
            incident = Incident(
                title=f"{a['name']} on {service}", severity=a.get("severity", "warning"), service=service,
                alert_name=a["name"], summary=a.get("summary", ""), status="open", detected_at=now,
                fault_scenario=fault_info.get("scenario"), fault_started_at=fault_info.get("started_at"),
                workflow=[{"ts": now.isoformat(), "step": "alert", "status": "done",
                           "detail": f"{a['name']} fired ({source})"}],
            )
            db.add(incident)
            db.flush()
            created = incident
            audit(db, "monitoring", "incident.created", "incident", incident.id,
                  f"Incident #{incident.id} opened from alert {a['name']}")
        elif a.get("summary") and a["summary"] not in incident.summary:
            incident.summary = (incident.summary + "; " + a["summary"]).strip("; ")
        db.add(Alert(fingerprint=fp, name=a["name"], severity=a.get("severity", "warning"), service=service,
                     summary=a.get("summary", ""), labels=a.get("labels", {}), source=source, starts_at=now,
                     received_at=now, incident_id=incident.id))
    db.flush()
    return created


def scan(ctx: RunContext) -> Incident | None:
    """Evaluate alert rules against the (simulated) environment and open an incident if needed."""
    firing = evaluate_rules(ctx.env)
    if not firing:
        return None
    faults = ctx.env.active_faults()
    fault_info = None
    if faults:
        fid, info = next(iter(faults.items()))
        from datetime import datetime

        started = info.get("started_at")
        fault_info = {"scenario": fid,
                      "started_at": datetime.fromisoformat(started) if isinstance(started, str) else started}
    with ctx.session() as db:
        inc = ingest_alerts(db, firing, "devnexa-monitor", ctx.clock.now(), fault_info)
        return inc.id if inc else None
