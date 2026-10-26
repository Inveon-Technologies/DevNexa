"""Background execution of agent workflows for the API."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..config import get_settings
from ..db import SessionLocal
from ..services.environment import get_environment
from ..simulation.catalog import FAULTS, SHIP_VIA_PIPELINE
from .cicd import create_run, run_pipeline
from .context import RealClock, RunContext
from .incident import run_incident
from .monitoring import scan

log = logging.getLogger("devnexa.runner")
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="devnexa-agent")
_incident_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def live_context() -> RunContext:
    s = get_settings()
    return RunContext(session_factory=SessionLocal, env=get_environment(), clock=RealClock(s.sim_step_delay))


def _safe(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except Exception:  # pragma: no cover - logged for operators
        log.exception("agent task failed")


def _incident_lock(incident_id: int) -> threading.Lock:
    with _locks_guard:
        return _incident_locks.setdefault(incident_id, threading.Lock())


def _pipeline_job(run_id: int):
    ctx = live_context()
    state = run_pipeline(ctx, run_id)
    if not state.get("failed_stage") and ctx.env.mode == "simulated":
        _monitor_job(get_settings().sim_step_delay * 3)


def _monitor_job(delay: float):
    time.sleep(delay)
    inc = scan(live_context())
    if inc:
        _incident_job(inc)


def _incident_job(incident_id: int, resume: str | None = None, **state):
    with _incident_lock(incident_id):
        run_incident(live_context(), incident_id, resume, **state)


def submit(fn, *args, sync: bool = False, **kw):
    if sync:
        return fn(*args, **kw)
    return _pool.submit(_safe, fn, *args, **kw)


def start_pipeline(run_id: int, sync: bool = False):
    return submit(_pipeline_job, run_id, sync=sync)


def start_incident(incident_id: int, resume: str | None = None, sync: bool = False, **state):
    return submit(_incident_job, incident_id, resume, sync=sync, **state)


def start_monitor_scan(delay: float | None = None, sync: bool = False):
    return submit(_monitor_job, get_settings().sim_step_delay * 2 if delay is None else delay, sync=sync)


def fake_sha(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()


def ship_fault(db, fault_id: str, mode: str | None = None, author: str = "fault-lab") -> dict:
    """Controlled fault injection: either ship the fault in a commit or inject it at runtime."""
    spec = FAULTS[fault_id]
    mode = mode or ("pipeline" if fault_id in SHIP_VIA_PIPELINE else "runtime")
    if mode == "pipeline" and spec.commit:
        run = create_run(db, commit_sha=fake_sha(f"{fault_id}{time.time()}"), message=spec.commit["message"],
                         files=spec.commit["files"], author=author, trigger="fault-lab", defect=fault_id)
        return {"mode": "pipeline", "run_id": run.id}
    get_environment().inject_fault(fault_id)
    return {"mode": "runtime"}
