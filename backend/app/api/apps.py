"""Deploy from GitHub: connect an account, pick a repo, deploy it to a URL, stream its logs, watch it."""

import asyncio
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import deploy, runner
from ..config import get_settings
from ..db import SessionLocal, get_db
from ..models import AppDeployment
from ..services import auth, github
from ..services.audit import audit
from .serialize import to_dict

router = APIRouter(prefix="/api", tags=["deploy from github"])


def require_admin(request: Request, db: Session = Depends(get_db)):
    """Deploying runs arbitrary code on the host, so these endpoints need a signed-in (allowed) GitHub user
    or the admin key. With neither configured, they are open only in simulated (local) mode."""
    s = get_settings()
    request.state.user = auth.current_user(request, db)
    if request.state.user:
        return
    given = request.headers.get("x-devnexa-key") or request.query_params.get("key") or ""
    if s.admin_token and given and hmac.compare_digest(given.encode(), s.admin_token.encode()):
        return
    if not s.admin_token and not auth.enabled():
        if s.is_live:
            raise HTTPException(403, "Set up Sign in with GitHub (DEVNEXA_GITHUB_CLIENT_ID/SECRET) or "
                                     "DEVNEXA_ADMIN_TOKEN in .env and restart the backend to enable deploys.")
        return
    raise HTTPException(401, "Sign in with GitHub first." if auth.enabled() else "Admin key required.")


def user(request: Request) -> str | None:
    return getattr(request.state, "user", None)


admin = [Depends(require_admin)]


def _gh(fn, *a):
    try:
        return fn(*a)
    except github.GitHubError as e:
        raise HTTPException(e.status if 400 <= e.status < 600 else 400, str(e)) from e


def dep_dict(d: AppDeployment, full: bool = False) -> dict:
    out = to_dict(d, exclude=("build_log", "env") if not full else ("env",))
    out["env_keys"] = sorted((d.env or {}).keys())
    m = d.metrics or {}
    out["metrics"] = m if full else {"last": m.get("last"), "history": (m.get("history") or [])[-30:],
                                     "stack": m.get("stack")}
    out["pending_approvals"] = sum(1 for r in d.remediations or [] if r["status"] == "pending_approval")
    return out


# ------------------------------------------------------------------ github
class ConnectRequest(BaseModel):
    token: str


@router.get("/github", dependencies=admin)
def github_status(request: Request, db: Session = Depends(get_db)):
    return github.connection(db, user(request))


@router.post("/github/connect", dependencies=admin)
def github_connect(req: ConnectRequest, request: Request, db: Session = Depends(get_db)):
    conn = _gh(github.connect, db, req.token)
    audit(db, conn.get("login") or "operator", "github.connect", "github", None, f"Connected GitHub as {conn['login']}")
    db.commit()
    return conn


@router.delete("/github", dependencies=admin)
def github_disconnect(request: Request, db: Session = Depends(get_db)):
    github.disconnect(db, user(request))
    audit(db, user(request) or "operator", "github.disconnect", "github", None, "Disconnected GitHub")
    db.commit()
    return {"connected": False}


def _token(db: Session, login: str | None = None) -> str:
    token = github.get_token(db, login)
    if not token:
        raise HTTPException(400, "Connect GitHub first.")
    return token


@router.get("/github/repos", dependencies=admin)
def github_repos(request: Request, db: Session = Depends(get_db)):
    return _gh(github.list_repos, _token(db, user(request)))


@router.get("/github/repos/{owner}/{repo}/branches", dependencies=admin)
def github_branches(owner: str, repo: str, request: Request, db: Session = Depends(get_db)):
    return _gh(github.list_branches, _token(db, user(request)), f"{owner}/{repo}")


# -------------------------------------------------------------------- apps
@router.get("/apps/config")
def apps_config():
    s = get_settings()
    return {"apps_domain": s.apps_domain or None, "url_mode": "subdomain" if s.apps_domain else "port",
            "docker_available": deploy.executor.available(), "admin_required": bool(s.admin_token) or s.is_live or auth.enabled(),
            "admin_configured": bool(s.admin_token), "monitor_interval_s": s.apps_monitor_interval,
            "github_login": auth.enabled()}


class DeployRequest(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    branch: str = "main"
    app: str | None = None
    root_dir: str = ""
    port: int | None = Field(default=None, ge=1, le=65535)
    env: dict[str, str] = Field(default_factory=dict)
    actor: str = "operator"


class DetectRequest(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    branch: str = "main"
    root_dir: str = ""


@router.post("/apps/detect", dependencies=admin)
def detect_stack(req: DetectRequest, request: Request, db: Session = Depends(get_db)):
    """Smart deploy preview: which language, framework, build tool, runtime version and port DevNexa found."""
    from ..services import buildplan

    token = github.get_token(db, user(request))
    if not token:
        raise HTTPException(400, "Connect GitHub first.")
    try:
        return deploy.detect_repo(token, req.repo, req.branch, req.root_dir)
    except buildplan.NotBuildable as e:
        raise HTTPException(400, str(e)) from e


@router.get("/apps", dependencies=admin)
def list_apps(db: Session = Depends(get_db)):
    deps = db.scalars(select(AppDeployment).order_by(AppDeployment.id.desc()).limit(500)).all()
    apps: dict[str, dict] = {}
    for d in deps:
        if (d.options or {}).get("preview_of"):
            continue  # issue-fix test URLs are listed under their app on the Live apps page
        a = apps.setdefault(d.app, {"app": d.app, "repo": d.repo, "latest": dep_dict(d), "live": None,
                                    "deployments": 0})
        a["deployments"] += 1
        if d.status == "running" and not a["live"]:
            a["live"] = dep_dict(d)
    return list(apps.values())


@router.get("/apps/deployments", dependencies=admin)
def list_app_deployments(app: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    q = select(AppDeployment).order_by(AppDeployment.id.desc()).limit(limit)
    if app:
        q = q.where(AppDeployment.app == app)
    return [dep_dict(d) for d in db.scalars(q)]


@router.post("/apps/deploy", status_code=201, dependencies=admin)
def deploy_repo(req: DeployRequest, request: Request, db: Session = Depends(get_db)):
    login = user(request)
    if not github.get_token(db, login):
        raise HTTPException(400, "Connect GitHub first.")
    options = {"port": req.port} if req.port else {}
    if login:
        options["gh_user"] = login  # clone, fix branches and PRs use this user's GitHub access
    dep = deploy.create_deployment(db, req.repo, req.branch, req.app, req.root_dir, req.env, options,
                                   actor=login or req.actor)
    db.commit()
    runner.submit(deploy.run_deploy, dep.id)
    return dep_dict(dep)


def _get(db: Session, dep_id: int) -> AppDeployment:
    d = db.get(AppDeployment, dep_id)
    if not d:
        raise HTTPException(404, "deployment not found")
    return d


@router.get("/apps/deployments/{dep_id}", dependencies=admin)
def get_app_deployment(dep_id: int, db: Session = Depends(get_db)):
    return dep_dict(_get(db, dep_id), full=True)


@router.post("/apps/deployments/{dep_id}/redeploy", status_code=201, dependencies=admin)
def redeploy(dep_id: int, request: Request, actor: str = "operator", db: Session = Depends(get_db)):
    actor = user(request) or actor
    d = _get(db, dep_id)
    opts = {k: v for k, v in (d.options or {}).items() if k != "reuse_image"}
    new = deploy.create_deployment(db, d.repo, d.branch, d.app, d.root_dir, dict(d.env or {}), opts, actor=actor)
    db.commit()
    runner.submit(deploy.run_deploy, new.id)
    return dep_dict(new)


@router.post("/apps/deployments/{dep_id}/stop", dependencies=admin)
def stop(dep_id: int, request: Request, actor: str = "operator", db: Session = Depends(get_db)):
    actor = user(request) or actor
    d = _get(db, dep_id)
    if d.container:
        deploy.executor.run(["docker", "rm", "-f", d.container])
    if d.status == "running":
        from ..services import releases

        releases.stop_replicas(db, d.app)
    d.status, d.health = "stopped", "stopped"
    audit(db, actor, "app.stop", "app_deployment", d.id, f"Stopped {d.app}")
    db.commit()
    return dep_dict(d)


class Decision(BaseModel):
    actor: str = "operator"


@router.post("/apps/deployments/{dep_id}/remediations/{rem_id}/{verdict}", dependencies=admin)
def decide(dep_id: int, rem_id: int, verdict: str, body: Decision, request: Request,
           db: Session = Depends(get_db)):
    if verdict not in ("approve", "reject"):
        raise HTTPException(404, "unknown verdict")
    _get(db, dep_id)
    try:
        return deploy.decide(dep_id, rem_id, verdict == "approve", user(request) or body.actor)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


# --------------------------------------------------------------- live logs
def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _build_stream(dep_id: int):
    sent = 0
    while True:
        with SessionLocal() as db:
            d = db.get(AppDeployment, dep_id)
            status, stored = d.status, d.build_log or ""
        buf = deploy.buffered_lines(dep_id, sent)
        if buf is None:  # not being built in this process: replay what was stored
            for ln in stored.splitlines()[sent:]:
                yield _sse("line", ln)
            yield _sse("end", {"status": status})
            return
        for ln in buf:
            yield _sse("line", ln)
        sent += len(buf)
        yield _sse("status", {"status": status})
        await asyncio.sleep(0.5)


async def _runtime_stream(container: str):
    proc = await asyncio.create_subprocess_exec(
        "docker", "logs", "-f", "--tail", "300", "--timestamps", container,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if not line:
                break
            yield _sse("line", line.decode(errors="replace").rstrip("\n"))
        yield _sse("end", {"status": "container stopped"})
    finally:
        if proc.returncode is None:
            proc.kill()


@router.get("/apps/deployments/{dep_id}/logs/stream", dependencies=admin)
def stream_logs(dep_id: int, source: str = Query("build", pattern="^(build|runtime)$"), db: Session = Depends(get_db)):
    d = _get(db, dep_id)
    if source == "runtime":
        if not d.container or not deploy.executor.available():
            raise HTTPException(400, "This deployment has no running container.")
        gen = _runtime_stream(d.container)
    else:
        gen = _build_stream(dep_id)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------- Caddy routing for app URLs
def _app_from_host(host: str) -> str | None:
    domain = get_settings().apps_domain
    host = host.split(":")[0].lower()
    if not domain or not host.endswith("." + domain):
        return None
    sub = host[: -len(domain) - 1]
    return sub if sub and "." not in sub else None


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font-family:system-ui;background:#0b1020;color:#cbd5e1;display:grid;place-items:center;
min-height:100vh;margin:0"><div style="max-width:480px;padding:24px;text-align:center"><h1 style="color:#fff">{title}</h1>
<p>{text}</p><p style="color:#64748b;font-size:13px">Served by DevNexa</p></div>"""


@router.get("/apps/route", include_in_schema=False)
def route(request: Request, db: Session = Depends(get_db)):
    """Caddy forward_auth hook: tells the proxy which container serves this hostname."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    app = _app_from_host(host)
    if not app:
        return HTMLResponse(PAGE.format(title="Not found", text="No app is deployed at this address."), 404)
    live = db.scalars(select(AppDeployment).where(AppDeployment.app == app, AppDeployment.status == "running")
                      .order_by(AppDeployment.id.desc())).first()
    if not live:
        latest = db.scalars(select(AppDeployment).where(AppDeployment.app == app)
                            .order_by(AppDeployment.id.desc())).first()
        if not latest:
            return HTMLResponse(PAGE.format(title="Not found", text="No app is deployed at this address."), 404)
        if latest.status in deploy.ACTIVE:
            return HTMLResponse(PAGE.format(title=f"{app} is deploying",
                                            text=f"Status: {latest.status}. This page will work once the deploy "
                                                 "finishes; refresh in a moment."), 503, headers={"Retry-After": "5"})
        return HTMLResponse(PAGE.format(title=f"{app} is not running",
                                        text="The last deploy did not succeed. Check its logs in DevNexa."), 503)
    from ..services import releases

    return Response(status_code=200, headers={"X-Devnexa-Upstream": releases.pick_upstream(db, live)})


@router.get("/apps/tls-check", include_in_schema=False)
def tls_check(domain: str, db: Session = Depends(get_db)):
    """Caddy on-demand TLS `ask` hook: only issue certificates for apps that exist."""
    app = _app_from_host(domain)
    if app and db.scalars(select(AppDeployment.id).where(AppDeployment.app == app)).first():
        return {"ok": True}
    raise HTTPException(404, "unknown app")
