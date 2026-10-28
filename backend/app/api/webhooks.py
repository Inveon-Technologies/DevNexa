"""Inbound integrations: GitHub push webhooks, GitHub Actions stage reports, and Alertmanager alerts."""

import hashlib
import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import deploy, runner
from ..agents.cicd import STAGES, create_run
from ..agents.monitoring import ingest_alerts
from ..agents.runner import start_incident, start_pipeline
from ..config import get_settings
from ..db import get_db
from ..models import AppDeployment, PipelineRun, StageRun
from ..services.analyzer import explain_ci_failure
from ..services.audit import audit

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _verify_signature(body: bytes, signature: str | None):
    secret = get_settings().github_webhook_secret
    if not secret:
        return
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "invalid webhook signature")


@router.post("/github")
async def github_push(request: Request, x_github_event: str = Header("push"),
                      x_hub_signature_256: str | None = Header(None), db: Session = Depends(get_db)):
    """GitHub push webhook. Apps deployed from the pushed repo and branch are redeployed; otherwise the push
    runs the DevNexa pipeline (real docker build, pytest and deploy of the sample app in live mode)."""
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)
    if x_github_event == "ping":
        return {"ok": True, "pong": True}
    if x_github_event != "push":
        return {"ok": True, "ignored": x_github_event}
    payload = await request.json()
    redeploys = redeploy_pushed_apps(db, (payload.get("repository") or {}).get("full_name", ""),
                                     (payload.get("ref") or "").split("/")[-1])
    if redeploys:
        db.commit()
        for dep_id in redeploys:
            runner.submit(deploy.run_deploy, dep_id)
        return {"ok": True, "redeploys": redeploys}
    head = payload.get("head_commit") or {}
    files = sorted({f for c in payload.get("commits", []) for k in ("added", "modified", "removed") for f in c.get(k, [])})
    run = create_run(db, commit_sha=head.get("id") or payload.get("after", "0" * 40),
                     message=(head.get("message") or "").splitlines()[0] if head.get("message") else "push",
                     files=files, author=(head.get("author") or {}).get("username") or (payload.get("pusher") or {}).get("name", "github"),
                     branch=(payload.get("ref") or "refs/heads/main").split("/")[-1], trigger="github-push",
                     repo=(payload.get("repository") or {}).get("full_name", "devnexa/sample-app"))
    db.commit()
    start_pipeline(run.id)
    return {"ok": True, "run_id": run.id}


def redeploy_pushed_apps(db: Session, repo: str, branch: str) -> list[int]:
    """Continuous deployment: every app running from the pushed repo and branch is rebuilt and redeployed."""
    if not repo or not branch:
        return []
    running = db.scalars(select(AppDeployment).where(AppDeployment.repo == repo, AppDeployment.branch == branch,
                                                     AppDeployment.status == "running")).all()
    ids = []
    for d in {d.app: d for d in sorted(running, key=lambda d: d.id)}.values():
        opts = {k: v for k, v in (d.options or {}).items() if k != "reuse_image"}
        new = deploy.create_deployment(db, d.repo, d.branch, d.app, d.root_dir, dict(d.env or {}), opts,
                                       actor="github-push")
        ids.append(new.id)
    return ids


class StageReport(BaseModel):
    """Posted by .github/workflows/devnexa-pipeline.yml after each real CI stage."""

    run_key: str                 # e.g. "<github run id>-<attempt>"
    commit_sha: str
    branch: str = "main"
    author: str = "github-actions"
    message: str = ""
    files: list[str] = []
    stage: str                   # build | test | release | deploy
    status: str                  # success | failed | running
    log: str = ""
    details: dict = {}
    repo: str = ""
    final: bool = False          # last stage this CI reports: on success the run is complete


@router.post("/github/actions")
def github_actions_report(rep: StageReport, x_devnexa_token: str | None = Header(None), db: Session = Depends(get_db)):
    s = get_settings()
    if s.github_webhook_secret and not hmac.compare_digest(x_devnexa_token or "", s.github_webhook_secret):
        raise HTTPException(401, "invalid token")
    if rep.stage not in {n for n, _ in STAGES}:
        raise HTTPException(400, "unknown stage")
    run = db.scalars(select(PipelineRun).where(PipelineRun.external_id == rep.run_key)).first()
    if run is None:
        run = create_run(db, commit_sha=rep.commit_sha, message=rep.message, files=rep.files, author=rep.author,
                         branch=rep.branch, trigger="github-actions", repo=rep.repo or "github")
        run.external_id, run.status, run.started_at = rep.run_key, "running", datetime.now(timezone.utc)
    st: StageRun = next(x for x in run.stages if x.name == rep.stage)
    now = datetime.now(timezone.utc)
    st.status, st.logs, st.details = rep.status, rep.log[-20000:], rep.details
    st.started_at = st.started_at or now
    if rep.status in ("success", "failed"):
        st.finished_at = now
    run.current_stage = rep.stage
    if rep.status == "failed":
        fb = explain_ci_failure(rep.stage, rep.log, run.changed_files)
        run.status, run.finished_at = "failed", now
        run.ai_feedback = f"{fb['summary']}\n\nSuggested fix: {fb['suggestion']}"
        for x in run.stages:
            if x.status == "pending":
                x.status = "skipped"
        audit(db, "github-actions", "pipeline.failed", "pipeline_run", run.id, f"{rep.stage} failed in GitHub Actions")
    elif rep.status == "success" and (rep.final or rep.stage == STAGES[-1][0]):
        run.status, run.finished_at, run.current_stage = "success", now, None
        for x in run.stages:
            if x.status in ("pending", "running"):
                x.status = "skipped"
    db.commit()
    return {"ok": True, "run_id": run.id, "ai_feedback": run.ai_feedback}


@router.post("/alertmanager")
async def alertmanager(request: Request, db: Session = Depends(get_db)):
    """Alertmanager webhook receiver (see infra/alertmanager/alertmanager.yml)."""
    payload = await request.json()
    alerts = []
    for a in payload.get("alerts", []):
        labels = a.get("labels", {})
        alerts.append({"name": labels.get("alertname", "Alert"), "severity": labels.get("severity", "warning"),
                       "summary": (a.get("annotations") or {}).get("summary", ""), "labels": labels,
                       "status": a.get("status", "firing"), "fingerprint": a.get("fingerprint")})
    inc = ingest_alerts(db, alerts, "alertmanager", datetime.now(timezone.utc))
    db.commit()
    if inc:
        start_incident(inc.id)
    return {"ok": True, "incident_id": inc.id if inc else None, "alerts": len(alerts)}
