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
from ..models import AppDeployment, Server
from ..services import auth, github, owners, servers
from ..services.audit import audit
from .serialize import to_dict

router = APIRouter(prefix="/api", tags=["deploy from github"])


def _resolve(request: Request, db: Session) -> owners.Viewer | None:
    """Who is calling: a signed-in, approved GitHub user (admin or member), the admin key, or open local mode.
    None when nobody valid is signed in."""
    s = get_settings()
    login = auth.current_user(request, db)
    if login:
        return owners.Viewer(login.lower(), auth.is_admin(db, login))
    given = request.headers.get("x-devnexa-key") or request.query_params.get("key") or ""
    if s.admin_token and given and hmac.compare_digest(given.encode(), s.admin_token.encode()):
        return owners.ADMIN
    if not s.admin_token and not auth.enabled() and not s.is_live:
        return owners.ADMIN  # local / simulated mode with no sign-in set up
    return None


def viewer(request: Request, db: Session) -> owners.Viewer:
    """The caller, without requiring sign-in (pages that are public but show per-user data)."""
    v = getattr(request.state, "viewer", None)
    if v is None:
        v = _resolve(request, db) or owners.NOBODY
        request.state.viewer = v
    return v


def require_user(request: Request, db: Session = Depends(get_db)):
    """Deploying runs arbitrary code on the host, so these endpoints need a signed-in, approved GitHub user or
    the admin key. With neither configured, they are open only in simulated (local) mode."""
    s = get_settings()
    v = _resolve(request, db)
    if v:
        request.state.viewer, request.state.user = v, v.login
        return
    if not s.admin_token and not auth.enabled():
        raise HTTPException(403, "Set up Sign in with GitHub (DEVNEXA_GITHUB_CLIENT_ID/SECRET) or "
                                 "DEVNEXA_ADMIN_TOKEN in .env and restart the backend to enable deploys.")
    login = auth.session_login(request, db)
    if login:
        u = auth.get_user(db, login)
        if u and u.status == "pending":
            raise HTTPException(403, "Your account is waiting for an admin to approve it.")
        raise HTTPException(403, f"@{login} may not use this DevNexa server.")
    raise HTTPException(401, "Sign in with GitHub first." if auth.enabled() else "Admin key required.")


def require_admin(request: Request, db: Session = Depends(get_db)):
    """Server-wide settings and other people's accounts: admins only."""
    require_user(request, db)
    if not request.state.viewer.is_admin:
        raise HTTPException(403, "Only an admin can do this.")


def user(request: Request) -> str | None:
    return getattr(request.state, "user", None)


def is_admin(request: Request) -> bool:
    v = getattr(request.state, "viewer", None)
    return bool(v and v.is_admin)


def check_app(request: Request, db: Session, app: str) -> None:
    """404 (not 403, so names of other people's apps do not leak) unless the caller may see `app`."""
    if not owners.can_see_app(db, request.state.viewer, app):
        raise HTTPException(404, "app not found")


def require_app(app: str, request: Request, db: Session = Depends(get_db)):
    """Path dependency for /apps/{app}/...: signed in, and the app is the caller's (or the caller is an admin)."""
    require_user(request, db)
    check_app(request, db, app)


def require_deployment(dep_id: int, request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    d = db.get(AppDeployment, dep_id)
    if not d:
        raise HTTPException(404, "deployment not found")
    check_app(request, db, d.app)


signed_in = [Depends(require_user)]
admin_only = [Depends(require_admin)]
app_scoped = [Depends(require_app)]
dep_scoped = [Depends(require_deployment)]
admin = signed_in  # older name


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


@router.get("/github", dependencies=signed_in)
def github_status(request: Request, db: Session = Depends(get_db)):
    return github.connection(db, user(request))


@router.post("/github/connect", dependencies=signed_in)
def github_connect(req: ConnectRequest, request: Request, db: Session = Depends(get_db)):
    conn = _gh(github.connect, db, req.token, user(request))
    audit(db, conn.get("login") or "operator", "github.connect", "github", None, f"Connected GitHub as {conn['login']}")
    db.commit()
    return conn


@router.delete("/github", dependencies=signed_in)
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


@router.get("/github/repos", dependencies=signed_in)
def github_repos(request: Request, db: Session = Depends(get_db)):
    return _gh(github.list_repos, _token(db, user(request)))


@router.get("/github/repos/{owner}/{repo}/branches", dependencies=signed_in)
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
    server_id: int | None = None  # None: the DevNexa host itself
    actor: str = "operator"


class DetectRequest(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    branch: str = "main"
    root_dir: str = ""


@router.post("/apps/detect", dependencies=signed_in)
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


@router.get("/apps", dependencies=signed_in)
def list_apps(request: Request, db: Session = Depends(get_db)):
    """The caller's apps (an admin sees everyone's, with the owner)."""
    seen = owners.visible_apps(db, request.state.viewer)
    deps = db.scalars(select(AppDeployment).order_by(AppDeployment.id.desc()).limit(2000)).all()
    apps: dict[str, dict] = {}
    for d in deps:
        if (d.options or {}).get("preview_of"):
            continue  # issue-fix test URLs are listed under their app on the Live apps page
        if seen is not None and d.app not in seen:
            continue
        a = apps.get(d.app)
        if a is None:
            a = apps[d.app] = {"app": d.app, "repo": d.repo, "latest": dep_dict(d), "live": None, "deployments": 0,
                               "owner": owners.app_owner(db, d.app)}
        a["deployments"] += 1
        if d.status == "running" and not a["live"]:
            a["live"] = dep_dict(d)
    return list(apps.values())


@router.get("/apps/deployments", dependencies=signed_in)
def list_app_deployments(request: Request, app: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    seen = owners.visible_apps(db, request.state.viewer)
    q = select(AppDeployment).order_by(AppDeployment.id.desc())
    if app:
        q = q.where(AppDeployment.app == app)
    if seen is not None:
        q = q.where(AppDeployment.app.in_(seen or {""}))
    return [dep_dict(d) for d in db.scalars(q.limit(limit))]


@router.post("/apps/deploy", status_code=201, dependencies=signed_in)
def deploy_repo(req: DeployRequest, request: Request, db: Session = Depends(get_db)):
    login = user(request)
    v = request.state.viewer
    name = deploy.slugify(req.app or req.repo.split("/")[-1])
    if owners.PREVIEW_RE.search(name):
        raise HTTPException(400, "Names ending in -pr-<n>, -fix-<n> or -test-<n> are kept for test URLs. "
                                 "Pick another app name.")
    if owners.app_exists(db, name) and not owners.can_see_app(db, v, name):
        raise HTTPException(409, f"The app name '{name}' is already used by another account. Pick another name.")
    try:
        owners.check_app_quota(db, v, name)
    except owners.QuotaExceeded as e:
        raise HTTPException(403, str(e)) from e
    options = {"port": req.port} if req.port else {}
    if req.server_id:
        server = db.get(Server, req.server_id)
        if not server or not owners.can_see_server(v, server):
            raise HTTPException(404, "server not found")
        if server.status == "offline":
            raise HTTPException(409, f"{server.name} did not answer its last check. Check it on the Servers page.")
        if server.status == "no_docker":
            raise HTTPException(409, f"{server.name} has no usable Docker. Install it from the Servers page.")
        options["server_id"] = server.id
    if not github.get_token(db, login):
        raise HTTPException(400, "Connect GitHub first.")
    if login:
        options["gh_user"] = login  # clone, fix branches and PRs use this user's GitHub access
        # A new app belongs to whoever deploys it; an admin redeploying someone's app keeps it theirs.
        options["owner"] = owners.app_owner(db, name) or login
    try:
        dep = deploy.create_deployment(db, req.repo, req.branch, req.app, req.root_dir, req.env, options,
                                       actor=login or req.actor)
    except owners.NameTaken as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    runner.submit(deploy.run_deploy, dep.id)
    return dep_dict(dep)


def _get(db: Session, dep_id: int) -> AppDeployment:
    d = db.get(AppDeployment, dep_id)
    if not d:
        raise HTTPException(404, "deployment not found")
    return d


@router.get("/apps/deployments/{dep_id}", dependencies=dep_scoped)
def get_app_deployment(dep_id: int, db: Session = Depends(get_db)):
    return dep_dict(_get(db, dep_id), full=True)


@router.post("/apps/deployments/{dep_id}/redeploy", status_code=201, dependencies=dep_scoped)
def redeploy(dep_id: int, request: Request, actor: str = "operator", db: Session = Depends(get_db)):
    actor = user(request) or actor
    d = _get(db, dep_id)
    opts = {k: v for k, v in (d.options or {}).items() if k != "reuse_image"}
    new = deploy.create_deployment(db, d.repo, d.branch, d.app, d.root_dir, dict(d.env or {}), opts, actor=actor)
    db.commit()
    runner.submit(deploy.run_deploy, new.id)
    return dep_dict(new)


@router.post("/apps/deployments/{dep_id}/stop", dependencies=dep_scoped)
def stop(dep_id: int, request: Request, actor: str = "operator", db: Session = Depends(get_db)):
    actor = user(request) or actor
    d = _get(db, dep_id)
    if d.container:
        try:
            with servers.use_for(d):
                deploy.executor.run(["docker", "rm", "-f", d.container])
        except servers.ServerError:
            pass  # the server was removed; nothing left to stop there
    if d.status == "running":
        from ..services import releases

        releases.stop_replicas(db, d.app)
    d.status, d.health = "stopped", "stopped"
    audit(db, actor, "app.stop", "app_deployment", d.id, f"Stopped {d.app}")
    db.commit()
    return dep_dict(d)


class Decision(BaseModel):
    actor: str = "operator"


@router.post("/apps/deployments/{dep_id}/remediations/{rem_id}/{verdict}", dependencies=dep_scoped)
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


async def _runtime_stream(container: str, env: dict | None = None):
    import os

    proc = await asyncio.create_subprocess_exec(
        "docker", "logs", "-f", "--tail", "300", "--timestamps", container,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, **(env or {})})
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


@router.get("/apps/deployments/{dep_id}/logs/stream", dependencies=dep_scoped)
def stream_logs(dep_id: int, source: str = Query("build", pattern="^(build|runtime)$"), db: Session = Depends(get_db)):
    d = _get(db, dep_id)
    if source == "runtime":
        if not d.container or not deploy.executor.available():
            raise HTTPException(400, "This deployment has no running container.")
        gen = _runtime_stream(d.container, servers.env_for((d.options or {}).get("server_id")))
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
