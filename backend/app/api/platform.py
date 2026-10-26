import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..agents.monitoring import OPEN_STATES
from ..agents.runner import ship_fault, start_monitor_scan, start_pipeline
from ..config import get_settings
from ..db import get_db, session_scope
from ..evaluation.runner import DEFAULT_CONFIG, VARIANTS, run_experiment
from ..models import (AuditLog, Deployment, ExperimentRun, Incident, KnowledgeEntry, PipelineRun, PolicyRule,
                      RemediationAction, TestCase)
from ..services import policy
from ..services.analyzer import CAUSES, get_analyzer
from ..services.audit import audit
from ..services.embeddings import get_embedder
from ..services.environment import get_environment
from ..simulation.catalog import ACTIONS, FAULTS, SHIP_VIA_PIPELINE
from .serialize import many, to_dict

router = APIRouter(prefix="/api", tags=["platform"])


@router.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@router.get("/system")
def system(db: Session = Depends(get_db)):
    s = get_settings()
    return {
        "version": __version__, "environment_mode": s.environment_mode, "llm_provider": s.llm_provider,
        "llm_model": s.llm_model if s.llm_provider != "heuristic" else None, "analyzer": get_analyzer().name,
        "database": db.bind.dialect.name, "vector_store": "pgvector" if db.bind.dialect.name == "postgresql" else
        "numpy (SQLite fallback)", "embedder": get_embedder().name, "embedding_dim": s.embedding_dim,
        "rag_top_k": s.rag_top_k, "max_reanalysis_attempts": s.max_reanalysis_attempts,
        "integrations": {
            "prometheus": s.prometheus_url if s.is_live else None, "loki": s.loki_url if s.is_live else None,
            "jaeger": s.jaeger_url if s.is_live else None, "sample_app": s.sample_app_url if s.is_live else None,
            "github_webhook": bool(s.github_webhook_secret),
        },
    }


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    runs = db.scalars(select(PipelineRun).order_by(PipelineRun.id)).all()
    incidents = db.scalars(select(Incident).order_by(Incident.id)).all()
    resolved = [i for i in incidents if i.resolved_at and i.status == "resolved"]
    mttrs = [(i.resolved_at - i.detected_at).total_seconds() for i in resolved]
    dets = [(i.detected_at - i.fault_started_at).total_seconds() for i in incidents if i.fault_started_at]
    finished = [r for r in runs if r.status in ("success", "failed")]

    days = defaultdict(lambda: {"success": 0, "failed": 0, "incidents": 0})
    start = now - timedelta(days=7)
    for r in runs:
        if r.created_at >= start and r.status in ("success", "failed"):
            days[r.created_at.date().isoformat()][r.status] += 1
    for i in incidents:
        if i.detected_at >= start:
            days[i.detected_at.date().isoformat()]["incidents"] += 1
    timeline = [{"date": (start + timedelta(days=k)).date().isoformat(),
                 **days[(start + timedelta(days=k)).date().isoformat()]} for k in range(8)]

    by_cause = Counter(CAUSES.get(i.confirmed_cause_id, {}).get("title", "Unconfirmed") for i in incidents)
    mttr_trend = [{"incident": i.id, "mttr_min": round((i.resolved_at - i.detected_at).total_seconds() / 60, 1),
                   "cause": CAUSES.get(i.confirmed_cause_id, {}).get("title")} for i in resolved[-15:]]
    pending = db.scalar(select(func.count()).select_from(RemediationAction)
                        .where(RemediationAction.status == "pending_approval"))
    auto = db.scalar(select(func.count()).select_from(RemediationAction).where(RemediationAction.decided_by == "policy"))
    active = db.scalars(select(Deployment).where(Deployment.status == "active").order_by(Deployment.id.desc())).first()
    return {
        "kpis": {
            "pipeline_runs": len(runs),
            "pipeline_success_rate": round(sum(r.status == "success" for r in finished) / len(finished), 3) if finished else None,
            "open_incidents": sum(i.status in OPEN_STATES for i in incidents),
            "resolved_incidents": len(resolved),
            "escalated_incidents": sum(i.status == "escalated" for i in incidents),
            "mttr_s": round(sum(mttrs) / len(mttrs), 1) if mttrs else None,
            "detection_s": round(sum(dets) / len(dets), 1) if dets else None,
            "pending_approvals": pending,
            "auto_remediations": auto,
            "knowledge_entries": db.scalar(select(func.count()).select_from(KnowledgeEntry)),
            "regression_tests": db.scalar(select(func.count()).select_from(TestCase).where(TestCase.is_regression.is_(True))),
            "first_attempt_rca_confirmed": round(sum(1 for i in resolved if i.attempts == 1) / len(resolved), 3) if resolved else None,
        },
        "timeline": timeline,
        "incidents_by_cause": [{"cause": k, "count": v} for k, v in by_cause.most_common()],
        "mttr_trend": mttr_trend,
        "active_deployment": to_dict(active) if active else None,
        "environment": environment_status(),
        "recent_activity": many(db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(12))),
    }


def environment_status() -> dict:
    env = get_environment()
    st = env.state
    checks = env.health()
    return {"mode": env.mode, "version": st.get("version"), "image": st.get("image"),
            "active_faults": list(st.get("active_faults", {}).keys()), "metrics": env.metrics(),
            "health": checks, "healthy": all(c["passed"] for c in checks), "config": st.get("config", {}),
            "events": st.get("events", [])[-10:]}


@router.get("/environment")
def environment():
    return environment_status()


@router.post("/environment/reset")
def reset_environment(actor: str = "operator", db: Session = Depends(get_db)):
    env = get_environment()
    env.clear_faults()
    audit(db, actor, "environment.faults_cleared", "environment", None, "All injected faults cleared")
    db.commit()
    return environment_status()


@router.get("/faults")
def list_faults():
    return [{"id": f.id, "title": f.title, "description": f.description, "severity": f.severity,
             "module": f.module, "tags": list(f.tags), "default_mode": "pipeline" if f.id in SHIP_VIA_PIPELINE else "runtime",
             "remediation": [ACTIONS[a].title for a in f.fix_actions],
             "remediation_risk": ACTIONS[f.fix_actions[0]].risk} for f in FAULTS.values()]


class InjectRequest(BaseModel):
    mode: str | None = None
    actor: str = "fault-lab"


@router.post("/faults/{fault_id}/inject")
def inject_fault(fault_id: str, req: InjectRequest, db: Session = Depends(get_db)):
    if fault_id not in FAULTS:
        raise HTTPException(404, "unknown fault scenario")
    res = ship_fault(db, fault_id, req.mode, req.actor)
    audit(db, req.actor, "fault.injected", "environment", None,
          f"Controlled fault '{FAULTS[fault_id].title}' injected via {res['mode']}", scenario=fault_id)
    db.commit()
    if res["mode"] == "pipeline":
        start_pipeline(res["run_id"])
    elif get_settings().environment_mode == "simulated":
        start_monitor_scan()
    return res


# ------------------------------------------------------------------ policy
@router.get("/policies")
def list_policies(db: Session = Depends(get_db)):
    return {"rules": many(db.scalars(select(PolicyRule).order_by(PolicyRule.risk_level, PolicyRule.action_type))),
            "autonomy": policy.get_autonomy(db)}


class RulePatch(BaseModel):
    allowlisted: bool | None = None
    enabled: bool | None = None
    risk_level: str | None = None
    actor: str = "operator"


@router.patch("/policies/{action_type}")
def patch_rule(action_type: str, body: RulePatch, db: Session = Depends(get_db)):
    rule = db.get(PolicyRule, action_type)
    if not rule:
        raise HTTPException(404, "unknown action")
    changes = body.model_dump(exclude_none=True, exclude={"actor"})
    if "risk_level" in changes and changes["risk_level"] not in ("low", "medium", "high"):
        raise HTTPException(400, "risk_level must be low, medium or high")
    if rule.risk_level == "high" and changes.get("allowlisted"):
        raise HTTPException(400, "high-risk actions always require human approval")
    for k, v in changes.items():
        setattr(rule, k, v)
    audit(db, body.actor, "policy.updated", "policy", action_type, f"Policy for {rule.title} updated", **changes)
    db.commit()
    return to_dict(rule)


@router.put("/policies/autonomy")
def put_autonomy(values: dict, db: Session = Depends(get_db)):
    merged = policy.set_autonomy(db, values)
    audit(db, values.get("actor", "operator"), "policy.autonomy", "policy", None, "Autonomy settings updated", **merged)
    db.commit()
    return merged


@router.get("/audit")
def audit_log(limit: int = 200, entity_type: str | None = None, db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    return many(db.scalars(q))


# -------------------------------------------------------------- experiments
@router.get("/experiments/defaults")
def experiment_defaults():
    return {"config": DEFAULT_CONFIG, "variants": {k: v["label"] for k, v in VARIANTS.items()},
            "scenarios": {k: f.title for k, f in FAULTS.items()}}


@router.get("/experiments")
def list_experiments(db: Session = Depends(get_db)):
    return [{**to_dict(e, exclude=("results",)), "variants": (e.results or {}).get("variants")}
            for e in db.scalars(select(ExperimentRun).order_by(ExperimentRun.id.desc()))]


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: int, db: Session = Depends(get_db)):
    e = db.get(ExperimentRun, exp_id)
    if not e:
        raise HTTPException(404, "experiment not found")
    return to_dict(e)


class ExperimentRequest(BaseModel):
    name: str | None = None
    config: dict = {}
    sync: bool = False


def _run_experiment_job(exp_id: int, cfg: dict):
    try:
        res = run_experiment(cfg)
        status = "completed"
    except Exception as e:  # pragma: no cover
        res, status = {"error": str(e)}, "failed"
    with session_scope() as db:
        e = db.get(ExperimentRun, exp_id)
        e.results, e.status, e.finished_at = res, status, datetime.now(timezone.utc)
        audit(db, "evaluator", f"experiment.{status}", "experiment", exp_id, f"Experiment #{exp_id} {status}")


@router.post("/experiments", status_code=201)
def create_experiment(req: ExperimentRequest, db: Session = Depends(get_db)):
    cfg = {**DEFAULT_CONFIG, **req.config}
    unknown = set(cfg["scenarios"]) - set(FAULTS)
    if unknown:
        raise HTTPException(400, f"unknown scenarios {sorted(unknown)}")
    cfg["repetitions"] = max(1, min(int(cfg["repetitions"]), 10))
    e = ExperimentRun(name=req.name or f"Fault-injection experiment ({len(cfg['scenarios'])} scenarios x "
                                         f"{cfg['repetitions']})", config=cfg)
    db.add(e)
    db.commit()
    if req.sync:
        _run_experiment_job(e.id, cfg)
    else:
        threading.Thread(target=_run_experiment_job, args=(e.id, cfg), daemon=True).start()
    db.refresh(e)
    return to_dict(e)
