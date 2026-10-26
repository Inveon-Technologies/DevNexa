import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..agents.cicd import create_run
from ..agents.runner import fake_sha, start_pipeline
from ..db import get_db
from ..models import Deployment, ModuleRisk, PipelineRun, TestCase
from ..services.audit import audit
from ..services.environment import get_environment
from ..services.feedback import changed_modules, pipeline_risk, prioritize_tests
from ..simulation.catalog import CI_DEFECTS, FAULTS, MODULES
from .serialize import many, to_dict

router = APIRouter(prefix="/api", tags=["ci/cd"])


class TriggerRequest(BaseModel):
    message: str = "feat: update orders service"
    files: list[str] = Field(default_factory=lambda: ["app/orders/routes.py"])
    branch: str = "main"
    author: str = "developer"
    strategy: str = "prioritized"
    defect: str | None = None


def run_summary(r: PipelineRun) -> dict:
    d = to_dict(r)
    d["stages"] = [{"name": s.name, "status": s.status, "agent": s.agent} for s in r.stages]
    end = r.finished_at
    d["duration_s"] = round((end - r.started_at).total_seconds(), 1) if end and r.started_at else None
    return d


@router.get("/pipelines")
def list_pipelines(limit: int = 50, db: Session = Depends(get_db)):
    runs = db.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)).all()
    return [run_summary(r) for r in runs]


@router.post("/pipelines", status_code=201)
def trigger_pipeline(req: TriggerRequest, db: Session = Depends(get_db)):
    if req.defect and req.defect not in CI_DEFECTS and req.defect not in FAULTS:
        raise HTTPException(400, f"unknown defect {req.defect}")
    if req.strategy not in ("prioritized", "full"):
        raise HTTPException(400, "strategy must be 'prioritized' or 'full'")
    run = create_run(db, commit_sha=fake_sha(req.message + str(time.time())), message=req.message,
                     files=req.files, author=req.author, branch=req.branch, trigger="manual",
                     strategy=req.strategy, defect=req.defect)
    db.commit()
    start_pipeline(run.id)
    return run_summary(run)


@router.get("/pipelines/{run_id}")
def get_pipeline(run_id: int, db: Session = Depends(get_db)):
    r = db.get(PipelineRun, run_id)
    if not r:
        raise HTTPException(404, "pipeline run not found")
    d = run_summary(r)
    d["stages"] = many(r.stages)
    d["tests"] = many(r.tests)
    d["modules"] = changed_modules(r.changed_files)
    d["deployment"] = next((to_dict(x) for x in db.scalars(select(Deployment).where(Deployment.run_id == r.id))), None)
    return d


@router.post("/pipelines/{run_id}/rerun", status_code=201)
def rerun_pipeline(run_id: int, db: Session = Depends(get_db)):
    r = db.get(PipelineRun, run_id)
    if not r:
        raise HTTPException(404, "pipeline run not found")
    run = create_run(db, commit_sha=r.commit_sha, message=r.commit_message, files=r.changed_files,
                     author=r.author, branch=r.branch, trigger="rerun", strategy=r.test_strategy, defect=None)
    db.commit()
    start_pipeline(run.id)
    return run_summary(run)


@router.get("/catalog")
def catalog():
    return {
        "modules": MODULES,
        "ci_defects": [{"id": d.id, "title": d.title, "stage": d.stage, "files": list(d.files)}
                       for d in CI_DEFECTS.values()],
    }


@router.get("/tests")
def list_tests(db: Session = Depends(get_db)):
    return many(db.scalars(select(TestCase).order_by(TestCase.is_regression.desc(), TestCase.module, TestCase.id)))


@router.get("/tests/prioritize")
def prioritize(files: str = Query("", description="comma-separated changed files"), db: Session = Depends(get_db)):
    """Used by GitHub Actions: returns the pytest node ids to run first for a change."""
    fl = [f.strip() for f in files.split(",") if f.strip()]
    ranked = prioritize_tests(db, fl)
    risk, factors = pipeline_risk(db, fl)
    total = sum(r["test"].duration_s for r in ranked)
    sel = [r for r in ranked if r["selected"]]
    return {
        "changed_modules": changed_modules(fl), "risk_score": risk, "risk_factors": factors,
        "selected": [r["test"].id for r in sel],
        "estimated_time_s": round(sum(r["test"].duration_s for r in sel), 1), "full_suite_time_s": round(total, 1),
        "ranking": [{"test_id": r["test"].id, "module": r["test"].module, "score": r["score"],
                     "selected": r["selected"], "reasons": r["reasons"], "kind": r["test"].kind,
                     "duration_s": r["test"].duration_s, "is_regression": r["test"].is_regression} for r in ranked],
    }


@router.get("/modules/risk")
def module_risk(db: Session = Depends(get_db)):
    risks = {r.module: r for r in db.scalars(select(ModuleRisk))}
    counts = dict(db.execute(select(TestCase.module, func.count()).group_by(TestCase.module)).all())
    regs = dict(db.execute(select(TestCase.module, func.count()).where(TestCase.is_regression.is_(True))
                           .group_by(TestCase.module)).all())
    out = []
    for m in MODULES:
        r = risks.get(m)
        out.append({"module": m, "risk_score": r.risk_score if r else 0.0, "incident_count": r.incident_count if r else 0,
                    "last_incident_at": r.last_incident_at.isoformat() if r and r.last_incident_at else None,
                    "tests": counts.get(m, 0), "regression_tests": regs.get(m, 0)})
    return sorted(out, key=lambda x: -x["risk_score"])


@router.get("/deployments")
def list_deployments(db: Session = Depends(get_db)):
    return many(db.scalars(select(Deployment).order_by(Deployment.id.desc()).limit(50)))


@router.post("/deployments/{dep_id}/rollback")
def manual_rollback(dep_id: int, actor: str = "operator", db: Session = Depends(get_db)):
    target = db.get(Deployment, dep_id)
    if not target:
        raise HTTPException(404, "deployment not found")
    current = db.scalars(select(Deployment).where(Deployment.status == "active").order_by(Deployment.id.desc())).first()
    if current and current.id == target.id:
        raise HTTPException(400, "deployment is already active")
    get_environment().rollback(target.version, target.image, target.commit_sha)
    if current:
        current.status = "rolled_back"
    target.status, target.health = "active", "healthy"
    audit(db, actor, "deployment.rollback", "deployment", target.id, f"Manual rollback to {target.version}")
    db.commit()
    return to_dict(target)
