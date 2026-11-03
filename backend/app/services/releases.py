"""Versions, backups and load balancing for deployed apps.

Every successful deploy of an app's own branch becomes a numbered release (v1, v2, ...): its image is tagged
`devnexa-app/<app>:v<N>` and the previous release is kept as a backup you can roll back to. Scheduled backups
snapshot the running container (`docker commit`). With the load balancer step on, extra replicas of the live
release run and the router spreads requests across them.

Per-app pipeline settings live in the kv_store under `app_settings:<app>`, so turning a step off or removing a
backup schedule is a setting change, not a code change.
"""

from __future__ import annotations

import itertools
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AppDeployment, AppRelease, KeyValue
from .audit import audit

log = logging.getLogger("devnexa.releases")

# The deploy pipeline, in order. `optional` steps can be switched off per app from the dashboard.
STEPS = [
    {"key": "clone", "title": "Clone the repo", "optional": False},
    {"key": "build", "title": "Build a Docker image", "optional": False},
    {"key": "version", "title": "Tag the image as the next version (v1, v2, ...)", "optional": False},
    {"key": "auto_backup", "title": "Keep earlier versions as backups", "optional": True},
    {"key": "persistent_data", "title": "Keep app files in a persistent /data volume (survives redeploys)", "optional": True},
    {"key": "smart_backup", "title": "Smart backups of /data and the database: on a schedule and before each new version",
     "optional": True},
    {"key": "health", "title": "Start and health-check", "optional": False},
    {"key": "load_balancer", "title": "Load balancer: run replicas and spread traffic", "optional": True},
    {"key": "issue_capture", "title": "Capture live issues from visitors' browsers", "optional": True},
    {"key": "auto_fix", "title": "Auto-fix: open a fix branch and a test URL for repeated issues", "optional": True},
]

DEFAULTS = {
    "steps": {"auto_backup": True, "persistent_data": True, "smart_backup": True, "load_balancer": False,
              "issue_capture": True, "auto_fix": True},
    "keep_versions": 5,
    "replicas": 2,
    "backup_schedules": [],
    # Smart data backups (services/backups.py). Retention keeps the newest `keep_last` plus one per day, week and
    # month; what it prunes goes to the trash first.
    "backup_policy": {"every_hours": 24, "keep_last": 7, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 3,
                      "skip_unchanged": True, "before_changes": True},
}
POLICY_LIMITS = {"every_hours": (1, 720), "keep_last": (1, 50), "keep_daily": (0, 60), "keep_weekly": (0, 52),
                 "keep_monthly": (0, 24)}


def now() -> datetime:
    return datetime.now(timezone.utc)


def _executor():
    from ..agents import deploy

    return deploy.executor


# ----------------------------------------------------------------- settings
def _key(app: str) -> str:
    return f"app_settings:{app}"


def get_settings(db: Session, app: str) -> dict:
    row = db.get(KeyValue, _key(app))
    saved = dict(row.value) if row else {}
    out = {**DEFAULTS, **saved}
    out["steps"] = {**DEFAULTS["steps"], **(saved.get("steps") or {})}
    out["backup_schedules"] = list(saved.get("backup_schedules") or [])
    out["backup_policy"] = {**DEFAULTS["backup_policy"], **(saved.get("backup_policy") or {})}
    return out


def save_settings(db: Session, app: str, value: dict) -> dict:
    row = db.get(KeyValue, _key(app))
    if row:
        row.value = value
    else:
        db.add(KeyValue(key=_key(app), value=value))
    db.flush()
    return get_settings(db, app)


def update_settings(db: Session, app: str, steps: dict | None = None, keep_versions: int | None = None,
                    replicas: int | None = None, actor: str = "operator", backup_policy: dict | None = None) -> dict:
    cur = get_settings(db, app)
    changes = []
    for k, v in (backup_policy or {}).items():
        if k in POLICY_LIMITS:
            lo, hi = POLICY_LIMITS[k]
            v = max(lo, min(hi, float(v) if k == "every_hours" else int(v)))
        elif k in ("skip_unchanged", "before_changes"):
            v = bool(v)
        else:
            continue
        if cur["backup_policy"].get(k) != v:
            cur["backup_policy"][k] = v
            changes.append(f"backup {k.replace('_', ' ')} {v}")
    for k, v in (steps or {}).items():
        if k in DEFAULTS["steps"] and cur["steps"].get(k) != bool(v):
            cur["steps"][k] = bool(v)
            changes.append(f"{k} {'on' if v else 'off'}")
    if keep_versions is not None:
        cur["keep_versions"] = max(1, min(20, int(keep_versions)))
        changes.append(f"keep {cur['keep_versions']} versions")
    if replicas is not None:
        cur["replicas"] = max(2, min(4, int(replicas)))
        changes.append(f"{cur['replicas']} replicas")
    out = save_settings(db, app, cur)
    if changes:
        audit(db, actor, "app.settings", "app", None, f"{app}: " + ", ".join(changes))
    return out


def add_schedule(db: Session, app: str, every_hours: float, keep: int, actor: str = "operator") -> dict:
    cur = get_settings(db, app)
    sched = {"id": secrets.token_hex(4), "every_hours": max(1.0, min(24 * 30, float(every_hours))),
             "keep": max(1, min(30, int(keep))), "created_at": now().isoformat(), "created_by": actor,
             "last_run": None}
    cur["backup_schedules"].append(sched)
    save_settings(db, app, cur)
    audit(db, actor, "app.backup_schedule.add", "app", None, f"{app}: back up every {sched['every_hours']}h, keep {sched['keep']}")
    return sched


def remove_schedule(db: Session, app: str, sched_id: str, actor: str = "operator") -> bool:
    cur = get_settings(db, app)
    before = len(cur["backup_schedules"])
    cur["backup_schedules"] = [s for s in cur["backup_schedules"] if s["id"] != sched_id]
    if len(cur["backup_schedules"]) == before:
        return False
    save_settings(db, app, cur)
    audit(db, actor, "app.backup_schedule.remove", "app", None, f"{app}: removed backup schedule {sched_id}")
    return True


# ------------------------------------------------------------------ queries
def live_deployment(db: Session, app: str) -> AppDeployment | None:
    return db.scalars(select(AppDeployment).where(AppDeployment.app == app, AppDeployment.status == "running")
                      .order_by(AppDeployment.id.desc())).first()


def list_releases(db: Session, app: str) -> list[AppRelease]:
    return list(db.scalars(select(AppRelease).where(AppRelease.app == app).order_by(AppRelease.id.desc())))


def is_preview(dep: AppDeployment) -> bool:
    opts = dep.options or {}
    return bool(opts.get("preview_of") or opts.get("fix_for"))  # test URLs: issue fixes and deploy-failure fixes


# ------------------------------------------------------------------ releases
def on_live(dep_id: int, env: dict, options: dict) -> None:
    """Called by the Deploy Agent once a deployment passed its health check and serves the URL."""
    from ..db import session_scope

    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        if not dep or is_preview(dep):
            return
        app = dep.app
        settings = get_settings(db, app)
        rel = _record_release(db, dep, options)
        live_image = rel.image if rel else dep.image
        removals = _retention(db, app, settings, live_image)
        lb = settings["steps"]["load_balancer"]
        primary = f"{dep.container}:{dep.port}"
        old_replicas = _get_lb(db, app).get("replicas", [])
        _set_upstreams(db, app, [primary], [])
    ex = _executor()
    for img in removals:
        ex.run(["docker", "rmi", img])
    for c in old_replicas:
        ex.run(["docker", "rm", "-f", c])
    if lb:
        start_replicas(dep_id, env, options)


def _record_release(db: Session, dep: AppDeployment, options: dict) -> AppRelease | None:
    if not dep.image:
        return None
    for r in db.scalars(select(AppRelease).where(AppRelease.app == dep.app, AppRelease.status == "live")):
        r.status = "backup"
    reused = options.get("reuse_image")
    if reused:  # a rollback or restore: that release becomes live again, no new version
        rel = db.scalars(select(AppRelease).where(AppRelease.app == dep.app, AppRelease.image == reused)
                         .order_by(AppRelease.id.desc())).first()
        if rel:
            rel.status = "live"
            audit(db, "deploy-agent", "app.release.restored", "app_release", rel.id, f"{dep.app} {rel.label} is live again")
            return rel
    version = (db.scalar(select(func.max(AppRelease.version)).where(AppRelease.app == dep.app)) or 0) + 1
    tag = f"devnexa-app/{dep.app}:v{version}"
    code, _ = _executor().run(["docker", "tag", dep.image, tag])
    source = options.get("source") or ("push" if dep.triggered_by == "github-push" else
                                       "agent-fix" if dep.parent_id else "deploy")
    rel = AppRelease(app=dep.app, version=version, label=f"v{version}", kind="release",
                     image=tag if code == 0 else dep.image, deployment_id=dep.id, commit_sha=dep.commit_sha,
                     commit_message=dep.commit_message, source=source, status="live", port=dep.port,
                     build_kind=dep.build_kind)
    db.add(rel)
    db.flush()
    audit(db, "deploy-agent", "app.release", "app_release", rel.id,
          f"{dep.app} v{version} is live ({(dep.commit_sha or '')[:7]} {dep.commit_message or ''})".strip())
    return rel


def _retention(db: Session, app: str, settings: dict, live_image: str | None) -> list[str]:
    """Mark releases beyond the retention limit as removed and return the image tags to delete."""
    keep = settings["keep_versions"] if settings["steps"]["auto_backup"] else 0
    backups = [r for r in db.scalars(select(AppRelease).where(
        AppRelease.app == app, AppRelease.kind == "release", AppRelease.status == "backup")
        .order_by(AppRelease.id.desc()))]
    out = []
    for r in backups[keep:]:
        r.status = "removed"
        if r.image != live_image:
            out.append(r.image)
    return out


def rollback(db: Session, app: str, release_id: int, actor: str) -> AppDeployment:
    from ..agents import deploy

    rel = db.get(AppRelease, release_id)
    if not rel or rel.app != app:
        raise KeyError("release not found")
    if rel.status == "removed":
        raise ValueError(f"{rel.label} was removed by the retention limit")
    base = live_deployment(db, app) or db.scalars(select(AppDeployment).where(AppDeployment.app == app)
                                                  .order_by(AppDeployment.id.desc())).first()
    if not base:
        raise KeyError("app not found")
    opts = {k: v for k, v in (base.options or {}).items() if k not in ("reuse_image", "source")}
    opts.update(reuse_image=rel.image, port=rel.port or base.port, build_kind=rel.build_kind, source="rollback")
    dep = deploy.create_deployment(db, base.repo, base.branch, app, base.root_dir, dict(base.env or {}), opts,
                                   actor=actor)
    audit(db, actor, "app.rollback", "app_release", rel.id, f"Roll {app} back to {rel.label} (deploy #{dep.id})")
    return dep


# ------------------------------------------------------------ load balancer
_rr = itertools.count()


def _lb_key(app: str) -> str:
    return f"app_lb:{app}"


def _get_lb(db: Session, app: str) -> dict:
    row = db.get(KeyValue, _lb_key(app))
    return dict(row.value) if row else {}


def _set_upstreams(db: Session, app: str, upstreams: list[str], replicas: list[str]) -> None:
    row = db.get(KeyValue, _lb_key(app))
    value = {"upstreams": upstreams, "replicas": replicas, "at": now().isoformat()}
    if row:
        row.value = value
    else:
        db.add(KeyValue(key=_lb_key(app), value=value))
    db.flush()


def pick_upstream(db: Session, live: AppDeployment) -> str:
    """Round-robin across the live deployment's healthy containers (the primary when the balancer is off)."""
    primary = f"{live.container}:{live.port}"
    ups = _get_lb(db, live.app).get("upstreams") or []
    ups = [u for u in ups if u.split(":")[0].startswith(live.container or "\0")] or [primary]
    return ups[next(_rr) % len(ups)]


def lb_status(db: Session, app: str) -> dict:
    lb = _get_lb(db, app)
    return {"upstreams": lb.get("upstreams", []), "replicas": lb.get("replicas", []), "updated_at": lb.get("at")}


def start_replicas(dep_id: int, env: dict, options: dict) -> list[str]:
    """Start replicas 2..N of a live deployment, health-check each, and add the healthy ones to the router."""
    from ..agents import deploy
    from ..config import get_settings as cfg
    from ..db import session_scope
    from . import backups

    s = cfg()
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        n = get_settings(db, dep.app)["replicas"]
        app, image, port, container = dep.app, dep.image, dep.port, dep.container
    ex = _executor()
    healthy, started = [f"{container}:{port}"], []
    for i in range(2, n + 1):
        name = f"{container}-r{i}"
        ex.run(["docker", "rm", "-f", name])
        cmd = ["docker", "run", "-d", "--name", name, "--network", s.apps_network,
               "--label", "devnexa.logs=true", "--label", f"devnexa.app={app}", "--label", f"devnexa.deployment={dep_id}",
               "--label", "devnexa.replica=true", "--memory", options.get("memory", s.apps_memory), "--cpus", s.apps_cpus,
               "-e", f"PORT={port}", "-e", "HOST=0.0.0.0"]
        extra_args, extra_env = backups.runtime(app)
        cmd += extra_args
        for k, v in {**extra_env, **env}.items():
            cmd += ["-e", f"{k}={v}"]
        code, _ = ex.run(cmd + [image])
        if code != 0:
            continue
        started.append(name)
        for _ in range(15):
            st, _ms = deploy.probe(f"http://{name}:{port}/")
            if st is not None and st < 500:
                healthy.append(f"{name}:{port}")
                break
            time.sleep(1)
    with session_scope() as db:
        _set_upstreams(db, app, healthy, started)
        audit(db, "deploy-agent", "app.load_balancer", "app_deployment", dep_id,
              f"{app}: {len(healthy)} containers behind the load balancer")
    return started


def stop_replicas(db: Session, app: str) -> None:
    lb = _get_lb(db, app)
    for c in lb.get("replicas", []):
        _executor().run(["docker", "rm", "-f", c])
    _set_upstreams(db, app, [], [])


def check_replicas(app: str) -> None:
    """Monitoring pass for replicas: drop dead ones from the router and restart them (low risk)."""
    from ..agents import deploy
    from ..db import session_scope

    with session_scope() as db:
        live = live_deployment(db, app)
        lb = _get_lb(db, app)
        if not live or not lb.get("replicas"):
            return
        port, replicas = live.port, lb["replicas"]
        primary = f"{live.container}:{live.port}"
    healthy = [primary]
    for c in replicas:
        st = deploy.inspect(c) or {}
        if not st.get("Running"):
            _executor().run(["docker", "restart", c])
            continue
        healthy.append(f"{c}:{port}")
    with session_scope() as db:
        _set_upstreams(db, app, healthy, replicas)


def apply_lb(app: str) -> None:
    """Bring the running replicas in line with the load balancer setting (after it was switched on or off)."""
    from ..db import session_scope

    with session_scope() as db:
        live = live_deployment(db, app)
        on = get_settings(db, app)["steps"]["load_balancer"]
        replicas = _get_lb(db, app).get("replicas", [])
        if not live:
            return
        dep_id, env, options = live.id, dict(live.env or {}), dict(live.options or {})
        _set_upstreams(db, app, [f"{live.container}:{live.port}"], [])
    for c in replicas:
        _executor().run(["docker", "rm", "-f", c])
    if on:
        start_replicas(dep_id, env, options)


# ------------------------------------------------------- scheduled backups
def due_schedules(db: Session, app: str, at: datetime | None = None) -> list[dict]:
    at = at or now()
    out = []
    for s in get_settings(db, app)["backup_schedules"]:
        last = datetime.fromisoformat(s["last_run"]) if s.get("last_run") else None
        if last is None or at - last >= timedelta(hours=s["every_hours"]):
            out.append(s)
    return out


def snapshot(app: str, sched_id: str | None = None, actor: str = "backup-schedule") -> AppRelease | None:
    """Back up the live container's current state as an image (`docker commit`)."""
    from ..db import session_scope

    with session_scope() as db:
        live = live_deployment(db, app)
        if not live or not live.container:
            return None
        container, dep_id, port, kind = live.container, live.id, live.port, live.build_kind
        sha, msg = live.commit_sha, live.commit_message
    stamp = now().strftime("%Y%m%d-%H%M%S")
    tag = f"devnexa-app/{app}:backup-{stamp}"
    code, out = _executor().run(["docker", "commit", "--pause=false", container, tag])
    if code != 0:
        log.warning("backup of %s failed: %s", app, out[-200:])
        return None
    with session_scope() as db:
        rel = AppRelease(app=app, label=f"backup {now().strftime('%Y-%m-%d %H:%M')}", kind="snapshot", image=tag,
                         deployment_id=dep_id, commit_sha=sha, commit_message=msg, port=port, build_kind=kind,
                         source="schedule" if sched_id else "manual", status="backup")
        db.add(rel)
        db.flush()
        cur = get_settings(db, app)
        keep = 5
        for s in cur["backup_schedules"]:
            if s["id"] == sched_id:
                s["last_run"] = now().isoformat()
                keep = s["keep"]
        save_settings(db, app, cur)
        old = [r for r in db.scalars(select(AppRelease).where(
            AppRelease.app == app, AppRelease.kind == "snapshot", AppRelease.status == "backup")
            .order_by(AppRelease.id.desc()))][keep:]
        drop = []
        for r in old:
            r.status = "removed"
            drop.append(r.image)
        audit(db, actor, "app.backup", "app_release", rel.id, f"{app}: snapshot {tag}")
        rel_id = rel.id
    for img in drop:
        _executor().run(["docker", "rmi", img])
    with session_scope() as db:
        return db.get(AppRelease, rel_id)


def run_due_backups() -> None:
    from ..db import session_scope

    with session_scope() as db:
        apps = [a for (a,) in db.execute(select(AppDeployment.app).where(AppDeployment.status == "running")
                                         .distinct())]
        due = [(a, s["id"]) for a in apps for s in due_schedules(db, a)]
    for app, sid in due:
        snapshot(app, sid)
