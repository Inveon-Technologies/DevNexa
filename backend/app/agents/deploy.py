"""Deploy Agent for user repos: clone -> detect -> docker build -> run -> health check, live logs,
runtime monitoring, and diagnose -> graded remediation -> verify -> learn when something breaks."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import AppDeployment
from ..services import app_doctor, buildplan, github, policy
from ..services.audit import audit

log = logging.getLogger("devnexa.deploy")
ACTIVE = ("queued", "cloning", "building", "starting")
ERROR_LINE = re.compile(r"\b(error|exception|traceback|fatal|panic|unhandled)\b", re.I)


def now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s[:40].strip("-") or "app")


# --------------------------------------------------------------- execution
class Executor:
    """Runs host commands (git, docker). Tests swap in a fake."""

    def available(self) -> bool:
        return shutil.which("docker") is not None

    def run(self, cmd: list[str], on_line: Callable[[str], None] | None = None, timeout: float | None = None,
            env: dict | None = None) -> tuple[int, str]:
        import os

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                errors="replace", env={**os.environ, **(env or {})})
        timer = threading.Timer(timeout, proc.kill) if timeout else None
        if timer:
            timer.start()
        out = []
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                out.append(line)
                if on_line:
                    on_line(line)
            proc.wait()
        finally:
            if timer:
                timer.cancel()
        return proc.returncode, "\n".join(out)


executor = Executor()


def probe(url: str) -> tuple[int | None, float]:
    """HTTP GET; returns (status or None if unreachable, latency ms)."""
    t = time.monotonic()
    try:
        r = httpx.get(url, timeout=5.0, follow_redirects=False)
        return r.status_code, round((time.monotonic() - t) * 1000, 1)
    except httpx.HTTPError:
        return None, round((time.monotonic() - t) * 1000, 1)


# -------------------------------------------------------------- live logs
_buffers: dict[int, list[str]] = {}
_buf_lock = threading.Lock()


def buffered_lines(dep_id: int, start: int = 0) -> list[str] | None:
    with _buf_lock:
        buf = _buffers.get(dep_id)
        return None if buf is None else buf[start:]


class DeployLog:
    def __init__(self, dep_id: int, secrets: list[str]):
        self.dep_id = dep_id
        self.secrets = [s for s in secrets if s]
        self.flushed_at = 0.0
        with _buf_lock:
            _buffers[dep_id] = []

    def __call__(self, line: str):
        for s in self.secrets:
            line = line.replace(s, "***")
        with _buf_lock:
            _buffers[self.dep_id].append(line)
        if time.monotonic() - self.flushed_at > 2:
            self.flush()

    def step(self, text: str):
        self(f"==> {text}")

    def text(self) -> str:
        with _buf_lock:
            return "\n".join(_buffers.get(self.dep_id, []))

    def flush(self):
        self.flushed_at = time.monotonic()
        with session_scope() as db:
            dep = db.get(AppDeployment, self.dep_id)
            dep.build_log = self.text()[-400000:]


def _update(dep_id: int, **fields) -> AppDeployment:
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        for k, v in fields.items():
            setattr(dep, k, v)
        return dep


class DeployFailed(Exception):
    def __init__(self, stage: str, message: str, output: str = ""):
        super().__init__(message)
        self.stage, self.output = stage, output


def public_url(app: str, host_port: int | None) -> str:
    s = get_settings()
    if s.apps_domain:
        return f"https://{app}.{s.apps_domain}"
    return f"http://{s.apps_public_host}:{host_port}"


# ------------------------------------------------------------------ deploy
def create_deployment(db, repo: str, branch: str, app: str | None = None, root_dir: str = "",
                      env: dict | None = None, options: dict | None = None, parent_id: int | None = None,
                      actor: str = "operator") -> AppDeployment:
    dep = AppDeployment(app=slugify(app or repo.split("/")[-1]), repo=repo, branch=branch,
                        root_dir=root_dir.strip("/"), env=env or {}, options=options or {}, parent_id=parent_id,
                        triggered_by=actor)
    db.add(dep)
    db.flush()
    audit(db, actor, "app.deploy", "app_deployment", dep.id, f"Deploy {repo}@{branch} as {dep.app}")
    return dep


def run_deploy(dep_id: int) -> None:
    s = get_settings()
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        token = github.get_token(db)
        repo, branch, app, root_dir = dep.repo, dep.branch, dep.app, dep.root_dir
        env, options = dict(dep.env or {}), dict(dep.options or {})
        dep.started_at = now()
    out = DeployLog(dep_id, [token])
    work = Path(s.apps_workdir) / str(dep_id)
    plan = None
    try:
        if not executor.available():
            raise DeployFailed("build", "Docker is not available to the DevNexa backend.")
        if options.get("reuse_image"):
            image, plan_port, kind = options["reuse_image"], int(options["port"]), options.get("build_kind")
            out.step(f"Re-using image {image} (no rebuild)")
        else:
            _update(dep_id, status="cloning")
            out.step(f"Cloning {repo} ({branch})")
            shutil.rmtree(work, ignore_errors=True)
            work.parent.mkdir(parents=True, exist_ok=True)
            auth = f"x-access-token:{token}@" if token else ""
            code, text = executor.run(["git", "clone", "--depth", "1", "--branch", branch,
                                       f"https://{auth}github.com/{repo}.git", str(work)], out, timeout=300)
            if code != 0:
                raise DeployFailed("clone", "git clone failed", text)
            _, sha = executor.run(["git", "-C", str(work), "rev-parse", "HEAD"])
            _, msg = executor.run(["git", "-C", str(work), "log", "-1", "--pretty=%s"])
            sha = sha.strip().splitlines()[-1] if sha.strip() else None
            _update(dep_id, commit_sha=sha, commit_message=msg.strip()[:500] or None)
            out(f"commit {sha} {msg.strip()}")

            root = work / root_dir if root_dir else work
            if not root.is_dir():
                raise DeployFailed("build", f"Folder {root_dir!r} does not exist in the repo.")
            try:
                plan = buildplan.detect(root, options)
            except buildplan.NotBuildable as e:
                raise DeployFailed("build", str(e), str(e)) from e
            for n in plan.notes:
                out(f"[detect] {n}")
            if plan.generated:
                (root / buildplan.GENERATED).write_text(plan.generated)
                out("[detect] generated Dockerfile:")
                for ln in plan.generated.strip().splitlines():
                    out(f"    {ln}")
            image = f"devnexa-app/{app}:{(sha or 'latest')[:12]}-{dep_id}"
            plan_port, kind = plan.port, plan.kind
            _update(dep_id, status="building", build_kind=kind, image=image, port=plan_port)
            out.step(f"Building image {image}")
            code, text = executor.run(["docker", "build", "--progress=plain", "-t", image,
                                       "-f", str(root / plan.dockerfile), str(root)], out,
                                      timeout=s.apps_build_timeout, env={"DOCKER_BUILDKIT": "1"})
            if code != 0:
                raise DeployFailed("build", "docker build failed", text)

        _start_and_verify(dep_id, app, image, plan_port, kind, env, options, out)
        out.step("Deployed")
    except DeployFailed as e:
        if e.stage == "start" and e.output:
            for ln in e.output.splitlines()[-80:]:
                out(f"[app] {ln}")
        out(f"!! {e}")
        out.flush()
        _fail(dep_id, e.stage, str(e), out.text(), work)
    except Exception as e:  # pragma: no cover - unexpected agent errors are still shown to the user
        log.exception("deploy %s crashed", dep_id)
        out(f"!! internal error: {e}")
        out.flush()
        _fail(dep_id, "build", f"internal error: {e}", out.text(), work)
    finally:
        out.flush()
        shutil.rmtree(work, ignore_errors=True)
        with _buf_lock:
            _buffers.pop(dep_id, None)


def _start_and_verify(dep_id, app, image, port, kind, env, options, out: DeployLog):
    s = get_settings()
    container = f"devnexa-app-{app}-{dep_id}"
    host_port = None if s.apps_domain else s.apps_host_port_base + dep_id % 1000
    _update(dep_id, status="starting", container=container, port=port, host_port=host_port, image=image,
            build_kind=kind)
    out.step(f"Starting container {container}")
    executor.run(["docker", "rm", "-f", container])
    cmd = ["docker", "run", "-d", "--name", container, "--network", s.apps_network,
           "--label", "devnexa.logs=true", "--label", f"devnexa.app={app}", "--label", f"devnexa.deployment={dep_id}",
           "--memory", options.get("memory", s.apps_memory), "--cpus", s.apps_cpus,
           "-e", f"PORT={port}", "-e", "HOST=0.0.0.0"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    if host_port:
        cmd += ["-p", f"{host_port}:{port}"]
    code, text = executor.run(cmd + [image], out)
    if code != 0:
        raise DeployFailed("start", "docker run failed", text)

    out.step(f"Health check on port {port}")
    deadline = time.monotonic() + s.apps_health_timeout
    status = None
    while True:  # at least one attempt, then retry until the deadline
        state = inspect(container)
        if state and not state.get("Running"):
            logs = container_logs(container, 200)
            raise DeployFailed("start", f"container exited with code {state.get('ExitCode')}"
                               + (" (out of memory)" if state.get("OOMKilled") else ""),
                               logs + ("\nOOMKilled" if state.get("OOMKilled") else ""))
        status, ms = probe(f"http://{container}:{port}/")
        if status is not None and status < 500:
            out(f"[health] GET / -> {status} in {ms} ms")
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    if status is None or status >= 500:
        logs = container_logs(container, 200)
        other = app_doctor.detected_port(logs)
        if other and other != port:
            out(f"[agent] nothing answers on {port}, logs show the app on {other}: re-routing (low risk, automatic)")
            st, ms = probe(f"http://{container}:{other}/")
            if st is not None and st < 500:
                _record(dep_id, {"action": "reroute_port", "title": app_doctor.APP_ACTIONS["reroute_port"].title,
                                 "risk": "low", "decision": "auto_execute", "status": "verified",
                                 "detail": f"Traffic sent to port {other} instead of {port}; GET / -> {st}"})
                if host_port:  # a host-port mapping cannot be changed on a running container
                    executor.run(["docker", "rm", "-f", container])
                    return _start_and_verify(dep_id, app, image, other, kind, env, options, out)
                port, status = other, st
                _update(dep_id, port=other)
        if status is None or status >= 500:
            raise DeployFailed("start", f"app did not answer on port {port} within {s.apps_health_timeout}s"
                               + (f" (last HTTP status {status})" if status else ""),
                               logs + ("\n__port_mismatch__" if other and other != port else ""))

    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        dep.status, dep.health, dep.finished_at = "running", "healthy", now()
        dep.url = public_url(app, host_port)
        older = db.scalars(select(AppDeployment).where(AppDeployment.app == app, AppDeployment.id != dep_id,
                                                       AppDeployment.status == "running")).all()
        for o in older:
            o.status, o.health = "replaced", "stopped"
        audit(db, "deploy-agent", "app.running", "app_deployment", dep_id, f"{app} is live at {dep.url}")
        parent = db.get(AppDeployment, dep.parent_id) if dep.parent_id else None
        if parent and parent.diagnosis:
            _verify_parent(db, parent, f"Deploy #{dep_id} succeeded")
        old_containers = [o.container for o in older if o.container]
    for c in old_containers:
        executor.run(["docker", "rm", "-f", c])
    out(f"[url] {public_url(app, host_port)}")


def _verify_parent(db, parent: AppDeployment, detail: str):
    rems = [dict(r) for r in parent.remediations or []]
    for r in rems:
        if r["status"] in ("running", "executed"):
            r.update(status="verified", verified_at=now().isoformat(), detail=r.get("detail", "") + f" · {detail}")
            app_doctor.remember(db, parent.diagnosis, parent.repo, r["action"], "fixed")
    parent.remediations = rems


def _fail(dep_id: int, stage: str, message: str, output: str, work: Path):
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        dep.status, dep.health, dep.error, dep.finished_at = "failed", "down", message, now()
        diag = app_doctor.diagnose(db, stage, output or message, work / dep.root_dir if work.exists() else None)
        dep.diagnosis = diag
        audit(db, "deploy-agent", "app.failed", "app_deployment", dep_id, f"{stage} failed: {diag['title']}")
        parent = db.get(AppDeployment, dep.parent_id) if dep.parent_id else None
        if parent and parent.diagnosis:
            rems = [dict(r) for r in parent.remediations or []]
            for r in rems:
                if r["status"] in ("running", "executed"):
                    r.update(status="failed", detail=r.get("detail", "") + f" · deploy #{dep_id} failed again")
                    app_doctor.remember(db, parent.diagnosis, parent.repo, r["action"], "did_not_fix")
            parent.remediations = rems
    remediate(dep_id)


# ------------------------------------------------------------- remediation
def _record(dep_id: int, entry: dict) -> int:
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        rems = list(dep.remediations or [])
        entry = {"id": len(rems), "at": now().isoformat(), **entry}
        rems.append(entry)
        dep.remediations = rems
        return entry["id"]


def _set_rem(dep_id: int, rem_id: int, **fields):
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        rems = [dict(r) for r in dep.remediations]
        rems[rem_id].update(fields)
        dep.remediations = rems


def _auto_depth(db, dep: AppDeployment) -> int:
    depth, cur = 0, dep
    while cur.parent_id:
        depth += 1
        cur = db.get(AppDeployment, cur.parent_id)
    return depth


def remediate(dep_id: int, diag: dict | None = None) -> None:
    """Graded autonomy: low-risk allowlisted fixes run now; medium/high wait for approval."""
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        diag = diag or dep.diagnosis
        if not diag:
            return
        action = diag["action"]
        if action == "rollback_app" or (diag.get("stage") == "runtime" and action == "escalate"):
            action = "rollback_app" if _last_good(db, dep) else "escalate"
        spec = app_doctor.APP_ACTIONS[action]
        auto = policy.get_autonomy(db)
        if spec.risk == "low" and spec.allowlisted and auto["auto_execute_low"] \
                and diag["confidence"] >= auto["min_confidence_for_auto"] and _auto_depth(db, dep) < 2:
            decision, reason = "auto_execute", "Low-risk allowlisted fix: running automatically."
        elif spec.risk == "low" and action == "escalate":
            decision, reason = "auto_execute", "No safe automatic fix: evidence handed to a developer."
        elif spec.risk == "low":
            decision, reason = "approval_required", "Automation limit reached or confidence too low; needs a person."
        else:
            decision, reason = "approval_required", f"{spec.risk.title()}-risk fix: waiting for human approval."
    rem_id = _record(dep_id, {"action": action, "title": spec.title, "risk": spec.risk, "decision": decision,
                              "reason": reason, "status": "pending_approval" if decision != "auto_execute" else "running",
                              "has_patch": bool(diag.get("patch"))})
    if decision == "auto_execute":
        execute(dep_id, rem_id, actor="policy")


def _last_good(db, dep: AppDeployment) -> AppDeployment | None:
    return db.scalars(select(AppDeployment).where(
        AppDeployment.app == dep.app, AppDeployment.id != dep.id, AppDeployment.image.is_not(None),
        AppDeployment.status.in_(("replaced", "running")), AppDeployment.health != "down",
    ).order_by(AppDeployment.id.desc())).first()


def execute(dep_id: int, rem_id: int, actor: str) -> dict:
    from . import runner

    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        rem = dict(dep.remediations[rem_id])
        diag = dep.diagnosis or {}
        audit(db, actor, f"app.remediation.{rem['action']}", "app_deployment", dep_id, rem["title"])
        base = dict(repo=dep.repo, branch=dep.branch, app=dep.app, root_dir=dep.root_dir, env=dict(dep.env or {}),
                    parent_id=dep.id, actor=actor)
        options = dict(dep.options or {})
        action = rem["action"]
        new = None
        result: dict = {}
        if action in ("retry_deploy", "use_npm_install", "prune_docker", "raise_memory"):
            if action == "use_npm_install":
                options["npm_install"] = True
            if action == "raise_memory":
                cur = re.sub(r"\D", "", options.get("memory", get_settings().apps_memory)) or "512"
                options["memory"] = f"{int(cur) * 2}m"
            new = create_deployment(db, options=options, **base)
            result = {"detail": f"Started deploy #{new.id}" + (" with " + json.dumps(
                {k: options[k] for k in ("npm_install", "memory") if k in options}) if action != "retry_deploy" else "")}
        elif action == "rollback_app":
            good = _last_good(db, dep)
            if not good:
                result = {"status": "failed", "detail": "No earlier healthy deployment to roll back to."}
            else:
                new = create_deployment(db, options={**options, "reuse_image": good.image, "port": good.port,
                                                     "build_kind": good.build_kind}, **base)
                result = {"detail": f"Rolling back to image of deploy #{good.id} as deploy #{new.id}"}
        elif action == "restart_app":
            pass
        elif action == "open_fix_pr":
            if not diag.get("patch"):
                result = {"status": "failed", "detail": "No patch was proposed."}
        elif action == "escalate":
            result = {"status": "escalated", "detail": "Developer needs to act; diagnosis and log evidence attached."}
        container, repo, branch, sha, app = dep.container, dep.repo, dep.branch, dep.commit_sha, dep.app
        token = github.get_token(db)
        new_id = new.id if new else None

    if action == "prune_docker":
        executor.run(["docker", "image", "prune", "-f"])
        executor.run(["docker", "builder", "prune", "-f"])
    if action == "restart_app":
        code, text = executor.run(["docker", "restart", container]) if container else (1, "no container")
        result = {"status": "executed" if code == 0 else "failed", "detail": text.strip()[-300:] or "restarted"}
        with session_scope() as db:
            d = db.get(AppDeployment, dep_id)
            d.restarts = (d.restarts or 0) + 1
            if code == 0:
                d.status, d.health = "running", "recovering"
    if action == "open_fix_pr" and diag.get("patch"):
        try:
            url = github.open_fix_pr(
                token, repo, branch, sha, diag["patch"], title=f"fix: {diag['title']} (DevNexa)",
                branch=f"devnexa/fix-{app}-{dep_id}",
                body=f"DevNexa's Deploy Agent proposes this change after deploy #{dep_id} failed.\n\n"
                     f"**Root cause:** {diag['root_cause']}\n\n**Suggestion:** {diag['suggestion']}\n\n"
                     f"**Confidence:** {diag['confidence']:.2f} ({diag.get('provider')})\n\n"
                     "```\n" + "\n".join(diag.get("evidence", [])[:12]) + "\n```\n\nApproved by " + actor + ".")
            result = {"status": "executed", "detail": "Pull request opened", "pr_url": url}
        except github.GitHubError as e:
            result = {"status": "failed", "detail": str(e)}
    result.setdefault("status", "executed")
    _set_rem(dep_id, rem_id, executed_at=now().isoformat(), decided_by=actor, **result)
    if new_id:
        runner.submit(run_deploy, new_id)
    return result


def decide(dep_id: int, rem_id: int, approve: bool, actor: str) -> dict:
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        if rem_id >= len(dep.remediations or []):
            raise KeyError("remediation not found")
        rem = dep.remediations[rem_id]
        if rem["status"] != "pending_approval":
            raise ValueError(f"remediation is {rem['status']}")
        if not approve:
            audit(db, actor, "app.remediation.rejected", "app_deployment", dep_id, rem["title"])
    if not approve:
        _set_rem(dep_id, rem_id, status="rejected", decided_by=actor, executed_at=now().isoformat())
        return {"status": "rejected"}
    _set_rem(dep_id, rem_id, status="running", decided_by=actor)
    return execute(dep_id, rem_id, actor)


# --------------------------------------------------------------- monitoring
def inspect(container: str) -> dict | None:
    code, text = executor.run(["docker", "inspect", "-f", "{{json .State}}", container])
    if code != 0:
        return None
    try:
        return json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def container_logs(container: str, tail: int = 200, since: str | None = None) -> str:
    cmd = ["docker", "logs", "--tail", str(tail)] + (["--since", since] if since else []) + [container]
    return executor.run(cmd)[1]


def _stats(container: str) -> dict:
    code, text = executor.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", container])
    try:
        d = json.loads(text.strip().splitlines()[-1]) if code == 0 else {}
    except (ValueError, IndexError):
        return {}
    mem = re.match(r"([\d.]+)\s*([KMG]i?B)", d.get("MemUsage", ""))
    mult = {"KiB": 1 / 1024, "KB": 1 / 1000, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1000}
    return {"cpu_pct": float(d.get("CPUPerc", "0").rstrip("%") or 0),
            "mem_mb": round(float(mem.group(1)) * mult.get(mem.group(2), 1), 1) if mem else None}


def monitor_tick() -> None:
    """One pass of the Monitoring Agent over every running app."""
    with session_scope() as db:
        deps = [(d.id, d.container, d.port, d.restarts or 0) for d in db.scalars(
            select(AppDeployment).where(AppDeployment.status == "running"))]
    s = get_settings()
    for dep_id, container, port, restarts in deps:
        state = inspect(container) or {}
        sample = {"t": now().isoformat(), "running": bool(state.get("Running"))}
        errors: list[str] = []
        if state.get("Running"):
            sample["status"], sample["latency_ms"] = probe(f"http://{container}:{port}/")
            sample.update(_stats(container))
            recent = container_logs(container, 300, since=f"{int(s.apps_monitor_interval) + 5}s")
            errors = [ln for ln in recent.splitlines() if ERROR_LINE.search(ln)]
            sample["errors"] = len(errors)
        healthy = sample["running"] and sample.get("status") is not None and sample["status"] < 500
        with session_scope() as db:
            dep = db.get(AppDeployment, dep_id)
            m = dict(dep.metrics or {})
            m["history"] = (m.get("history", []) + [sample])[-60:]
            m["last"] = sample
            m["consecutive_failures"] = 0 if healthy else m.get("consecutive_failures", 0) + 1
            if errors_seen := sample.get("errors"):
                m["recent_errors"] = (m.get("recent_errors", []) + errors[-5:])[-20:]
            dep.metrics = m
            prev = dep.health
            dep.health = "healthy" if healthy and not errors_seen else ("degraded" if healthy else "down")
            if healthy and prev == "recovering":
                _verify_restart(db, dep)
            trip = not sample["running"] or m["consecutive_failures"] >= 3
            if trip:
                m["consecutive_failures"] = 0
                dep.metrics = dict(m)
        if trip:
            _runtime_incident(dep_id, container, state, restarts)


def _verify_restart(db, dep: AppDeployment):
    rems = [dict(r) for r in dep.remediations or []]
    for r in rems:
        if r["action"] == "restart_app" and r["status"] == "executed":
            r.update(status="verified", verified_at=now().isoformat())
            app_doctor.remember(db, r.get("diagnosis") or dep.diagnosis or {}, dep.repo, "restart_app", "fixed")
    dep.remediations = rems


def _runtime_incident(dep_id: int, container: str, state: dict, restarts: int) -> None:
    s = get_settings()
    logs = container_logs(container, 200) + ("\nOOMKilled" if state.get("OOMKilled") else "")
    with session_scope() as db:
        dep = db.get(AppDeployment, dep_id)
        diag = app_doctor.diagnose(db, "runtime", logs)
        if diag["action"] not in ("raise_memory", "open_fix_pr"):
            diag.update(action="restart_app" if restarts < s.apps_max_auto_restarts else "rollback_app")
        a = app_doctor.APP_ACTIONS[diag["action"]]
        diag.update(action_title=a.title, risk=a.risk)
        dep.diagnosis = diag
        dep.health = "down"
        audit(db, "monitoring-agent", "app.incident", "app_deployment", dep_id, f"{dep.app}: {diag['title']}")
    remediate(dep_id, diag)


_monitor_started = False


def start_monitor() -> None:
    global _monitor_started
    if _monitor_started or not executor.available():
        return
    _monitor_started = True

    def loop():
        while True:
            time.sleep(get_settings().apps_monitor_interval)
            try:
                monitor_tick()
            except Exception:  # pragma: no cover
                log.exception("app monitor tick failed")

    threading.Thread(target=loop, name="devnexa-app-monitor", daemon=True).start()
