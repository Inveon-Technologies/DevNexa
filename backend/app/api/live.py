"""After deploy: live issues, fix branches with test URLs, versions and backups, and per-app pipeline settings."""

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import fixer, runner
from ..agents import deploy as deploy_agent
from ..db import get_db
from ..models import AppDeployment, AppIssue, AppPreview, AppRelease
from ..services import github, issues, releases
from ..services.audit import audit
from .apps import _app_from_host, admin, dep_dict
from .serialize import to_dict

router = APIRouter(prefix="/api/live", tags=["live apps"])
public = APIRouter(tags=["live issue capture"])
PREVIEW_RE = re.compile(r"-(pr|test|fix)-\d+$")


def _gh(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except github.GitHubError as e:
        raise HTTPException(e.status if 400 <= e.status < 600 else 400, str(e)) from e


def _app_or_404(db: Session, app: str) -> AppDeployment:
    d = db.scalars(select(AppDeployment).where(AppDeployment.app == app).order_by(AppDeployment.id.desc())).first()
    if not d:
        raise HTTPException(404, "app not found")
    return d


def issue_dict(i: AppIssue) -> dict:
    out = to_dict(i, exclude=("fingerprint",))
    out["kind_title"] = issues.KINDS.get(i.kind, ("", i.kind))[1]
    return out


# ------------------------------------------------------------------ overview
@router.get("/apps", dependencies=admin)
def live_apps(db: Session = Depends(get_db)):
    """Every deployed app (test URLs excluded) with its open issue counts, live version and test URLs."""
    names = [a for (a,) in db.execute(select(AppDeployment.app).distinct()) if not PREVIEW_RE.search(a)]
    out = []
    for name in sorted(names):
        live = releases.live_deployment(db, name)
        rel = db.scalars(select(AppRelease).where(AppRelease.app == name, AppRelease.status == "live")).first()
        out.append({"app": name, "live": dep_dict(live) if live else None, "version": rel.label if rel else None,
                    "issues": issues.summary(db, name),
                    "previews": sum(1 for p in db.scalars(select(AppPreview).where(AppPreview.app == name))
                                    if fixer.sync(db, p).status in ("deploying", "ready"))})
    db.commit()
    return out


@router.get("/apps/{app}", dependencies=admin)
def app_overview(app: str, db: Session = Depends(get_db)):
    latest = _app_or_404(db, app)
    live = releases.live_deployment(db, app)
    out = {"app": app, "repo": latest.repo, "branch": latest.branch, "live": dep_dict(live) if live else None,
           "latest": dep_dict(latest), "settings": releases.get_settings(db, app), "steps": releases.STEPS,
           "load_balancer": releases.lb_status(db, app), "issues": issues.summary(db, app),
           "agent_script": issues.script_tag(app)}
    return out


# -------------------------------------------------------------------- issues
@router.get("/apps/{app}/issues", dependencies=admin)
def list_issues(app: str, status: str = "active", db: Session = Depends(get_db)):
    q = select(AppIssue).where(AppIssue.app == app).order_by(AppIssue.last_seen.desc()).limit(300)
    rows = db.scalars(q).all()
    if status == "active":
        rows = [r for r in rows if r.status in ("open", "fixing", "in_preview")]
    elif status != "all":
        rows = [r for r in rows if r.status == status]
    return [issue_dict(r) for r in rows]


class FixRequest(BaseModel):
    actor: str = "operator"
    hint: str | None = Field(default=None, max_length=2000)


@router.post("/apps/{app}/issues/{issue_id}/fix", status_code=202, dependencies=admin)
def fix_issue(app: str, issue_id: int, body: FixRequest, db: Session = Depends(get_db)):
    issue = db.get(AppIssue, issue_id)
    if not issue or issue.app != app:
        raise HTTPException(404, "issue not found")
    if issue.status in ("fixing", "in_preview"):
        raise HTTPException(409, "A fix for this issue is already in progress.")
    if not github.get_token(db):
        raise HTTPException(400, "Connect GitHub first.")
    issue.status, issue.fix = "fixing", {"status": "queued", "started_by": body.actor}
    db.commit()
    runner.submit(fixer.start_fix, issue_id, body.actor, body.hint)
    return issue_dict(db.get(AppIssue, issue_id))


class StatusRequest(BaseModel):
    status: str = Field(pattern="^(open|ignored|resolved)$")
    actor: str = "operator"


@router.post("/apps/{app}/issues/{issue_id}/status", dependencies=admin)
def set_issue_status(app: str, issue_id: int, body: StatusRequest, db: Session = Depends(get_db)):
    issue = db.get(AppIssue, issue_id)
    if not issue or issue.app != app:
        raise HTTPException(404, "issue not found")
    issue.status = body.status
    audit(db, body.actor, f"app.issue.{body.status}", "app_issue", issue_id, issue.message[:120])
    db.commit()
    return issue_dict(issue)


@router.post("/apps/{app}/scan", dependencies=admin)
def scan_pages(app: str, db: Session = Depends(get_db)):
    live = releases.live_deployment(db, app)
    if not live:
        raise HTTPException(400, "The app is not running.")
    # Scan through the container directly: it works whether or not the public URL / DNS is set up yet.
    res = issues.scan(db, app, f"http://{live.container}:{live.port}/")
    audit(db, "operator", "app.scan", "app", None, f"{app}: scanned {res['pages']} pages, {res['issues']} issues")
    db.commit()
    return res


# --------------------------------------------------------- test URLs / PRs
@router.get("/apps/{app}/previews", dependencies=admin)
def list_previews(app: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(AppPreview).where(AppPreview.app == app).order_by(AppPreview.id.desc()).limit(50)).all()
    out = [fixer.preview_dict(db, p) for p in rows]
    db.commit()
    return out


class PreviewRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=160, pattern=r"^[\w./-]+$")
    actor: str = "operator"


@router.post("/apps/{app}/previews", status_code=201, dependencies=admin)
def create_preview(app: str, body: PreviewRequest, db: Session = Depends(get_db)):
    _app_or_404(db, app)
    try:
        pid = _gh(fixer.preview_branch, db, app, body.branch, body.actor)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.expire_all()
    return fixer.preview_dict(db, db.get(AppPreview, pid))


class Verdict(BaseModel):
    actor: str = "operator"
    reason: str = ""


@router.post("/previews/{preview_id}/{verdict}", dependencies=admin)
def decide_preview(preview_id: int, verdict: str, body: Verdict, db: Session = Depends(get_db)):
    if verdict not in ("approve", "reject"):
        raise HTTPException(404, "unknown verdict")
    try:
        res = _gh(fixer.approve if verdict == "approve" else fixer.reject, db, preview_id, body.actor,
                  *(() if verdict == "approve" else (body.reason,)))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    if res.get("deployment_id"):
        runner.submit(deploy_agent.run_deploy, res["deployment_id"])
    if res.get("resolve"):  # merge conflicts: resolve, re-test on the test URL, then merge and roll out
        runner.submit(fixer.resolve_and_merge, res["resolve"], body.actor)
    return res


# ------------------------------------------------------- versions / backups
@router.get("/apps/{app}/releases", dependencies=admin)
def list_releases(app: str, db: Session = Depends(get_db)):
    return [to_dict(r) for r in releases.list_releases(db, app)]


class ActorBody(BaseModel):
    actor: str = "operator"


@router.post("/apps/{app}/releases/{release_id}/rollback", status_code=201, dependencies=admin)
def rollback(app: str, release_id: int, body: ActorBody, db: Session = Depends(get_db)):
    try:
        dep = releases.rollback(db, app, release_id, body.actor)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    runner.submit(deploy_agent.run_deploy, dep.id)
    return dep_dict(dep)


@router.post("/apps/{app}/backups", status_code=201, dependencies=admin)
def backup_now(app: str, body: ActorBody, db: Session = Depends(get_db)):
    if not releases.live_deployment(db, app):
        raise HTTPException(400, "The app is not running.")
    db.close()
    rel = releases.snapshot(app, None, body.actor)
    if not rel:
        raise HTTPException(500, "docker commit failed; see the backend logs.")
    return to_dict(rel)


# ------------------------------------------------------- pipeline settings
class SettingsRequest(BaseModel):
    steps: dict[str, bool] | None = None
    keep_versions: int | None = Field(default=None, ge=1, le=20)
    replicas: int | None = Field(default=None, ge=2, le=4)
    backup_policy: dict | None = None
    actor: str = "operator"


@router.put("/apps/{app}/settings", dependencies=admin)
def update_settings(app: str, body: SettingsRequest, db: Session = Depends(get_db)):
    _app_or_404(db, app)
    before = releases.get_settings(db, app)
    out = releases.update_settings(db, app, body.steps, body.keep_versions, body.replicas, body.actor,
                                   body.backup_policy)
    db.commit()
    lb_changed = before["steps"]["load_balancer"] != out["steps"]["load_balancer"] or \
        (out["steps"]["load_balancer"] and before["replicas"] != out["replicas"])
    if lb_changed:
        runner.submit(releases.apply_lb, app)
    return out


class ScheduleRequest(BaseModel):
    every_hours: float = Field(ge=1, le=720)
    keep: int = Field(default=7, ge=1, le=30)
    actor: str = "operator"


@router.post("/apps/{app}/backup-schedules", status_code=201, dependencies=admin)
def add_schedule(app: str, body: ScheduleRequest, db: Session = Depends(get_db)):
    _app_or_404(db, app)
    s = releases.add_schedule(db, app, body.every_hours, body.keep, body.actor)
    db.commit()
    return s


@router.delete("/apps/{app}/backup-schedules/{sched_id}", dependencies=admin)
def remove_schedule(app: str, sched_id: str, actor: str = "operator", db: Session = Depends(get_db)):
    if not releases.remove_schedule(db, app, sched_id, actor):
        raise HTTPException(404, "schedule not found")
    db.commit()
    return {"removed": sched_id}


# ---------------------------------------------- browser issue capture (public)
@public.get("/__devnexa/agent.js", include_in_schema=False)
def agent_js():
    return Response(issues.AGENT_JS, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})


@public.post("/__devnexa/report", include_in_schema=False)
async def report(request: Request, db: Session = Depends(get_db)):
    """Called by agent.js from visitors' browsers. Unauthenticated by design, so it is size- and rate-limited and
    only accepts reports for apps that exist."""
    body = await request.body()
    if len(body) > 20000:
        raise HTTPException(413, "report too large")
    try:
        data = json.loads(body or b"{}")
    except ValueError as e:
        raise HTTPException(400, "invalid JSON") from e
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid report")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    app = _app_from_host(host) or str(data.get("app") or "")
    client = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if not app or not issues.rate_ok(f"{client}|{app}"):
        return Response(status_code=204)
    if not db.scalars(select(AppDeployment.id).where(AppDeployment.app == app)).first():
        return Response(status_code=204)
    issue = issues.ingest_report(db, app, data)
    auto = bool(issue and fixer.should_auto_fix(db, issue))
    if auto:
        issue.fix = {"status": "queued", "started_by": "fix-agent", "at": fixer.now().isoformat()}
    db.commit()
    if auto:
        runner.submit(fixer.start_fix, issue.id, "fix-agent")
    return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
