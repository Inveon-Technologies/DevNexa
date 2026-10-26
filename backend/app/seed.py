"""Initial data: policies, test suite, a stable deployment, and a short history produced by actually
running the agent workflows on the simulated environment (so the dashboard and incident memory start
with realistic, internally consistent records)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .agents.cicd import create_run, run_pipeline
from .agents.context import RunContext, SimClock
from .agents.incident import run_incident
from .agents.monitoring import scan
from .agents.runner import fake_sha
from .db import SessionLocal, session_scope
from .models import Deployment, PipelineRun
from .services.environment import DEFAULT_STATE, get_environment
from .services.feedback import seed_test_suite
from .services.policy import seed_policies
from .simulation.catalog import FAULTS, MODULES, SERVICE, SHIP_VIA_PIPELINE

CLEAN_COMMITS = [
    ("feat(orders): add order notes field", ["app/orders/routes.py", "app/orders/serializers.py"]),
    ("fix(checkout): round discount to 2 decimals", ["app/checkout/routes.py"]),
    ("refactor(db): extract repository helpers", ["app/db/repository.py"]),
    ("chore: bump httpx to 0.27", ["requirements.txt"]),
    ("feat(auth): shorter token TTL for admin", ["app/auth/tokens.py"]),
    ("docs: health endpoint description", ["app/health.py"]),
    ("perf(cache): tune TTL", ["app/cache/lru.py"]),
    ("fix(payments): log gateway request ids", ["app/integrations/payments_client.py"]),
]
AUTHORS = ["adarsh", "harshita", "ci-bot", "devnexa-demo"]


def seed_reference_data() -> None:
    with session_scope() as db:
        seed_policies(db)
        seed_test_suite(db)
        if not db.scalars(select(Deployment)).first():
            st = DEFAULT_STATE
            db.add(Deployment(service=SERVICE, version=st["version"], image=st["image"], commit_sha=st["commit_sha"],
                              status="active", health="healthy", is_stable=True,
                              deployed_at=datetime.now(timezone.utc) - timedelta(days=7),
                              notes="Initial release"))


def seed_history() -> None:
    """Replay a few days of activity through the real agent graphs with a simulated clock."""
    with session_scope() as db:
        if db.scalars(select(PipelineRun)).first():
            return
    env = get_environment()
    clock = SimClock(datetime.now(timezone.utc) - timedelta(days=6))
    env._now_fn = clock.now  # type: ignore[attr-defined]
    rng = random.Random(42)

    def approver(_action):
        return True, rng.uniform(180, 600)

    ctx = RunContext(session_factory=SessionLocal, env=env, clock=clock, approver=approver, rng_seed=3,
                     observability_gap=0.2)

    def commit(message, files, defect=None):
        with ctx.session() as db:
            run = create_run(db, commit_sha=fake_sha(message + str(clock.now())), message=message, files=files,
                             author=rng.choice(AUTHORS), trigger="github-push", defect=defect, now=clock.now())
            rid = run.id
        return run_pipeline(ctx, rid)

    def incident_cycle(fault_id):
        if fault_id in SHIP_VIA_PIPELINE:
            state = commit(FAULTS[fault_id].commit["message"], FAULTS[fault_id].commit["files"], fault_id)
            if state.get("failed_stage"):
                return
        else:
            env.inject_fault(fault_id)
        clock.advance(rng.uniform(60, 150))
        inc = scan(ctx)
        if inc:
            run_incident(ctx, inc)
        env.clear_faults()

    plan = [
        ("commit", 0), ("commit", 1), ("fault", "app_exception"), ("commit", 2), ("commit", "build_dependency_conflict"),
        ("fault", "dependency_failure"), ("commit", 3), ("fault", "memory_leak"), ("commit", 4),
        ("commit", "unit_test_regression"), ("fault", "slow_query"), ("commit", 5), ("fault", "invalid_config"),
        ("commit", 6), ("fault", "db_pool_exhaustion"), ("commit", 7),
    ]
    for kind, arg in plan:
        clock.advance(rng.uniform(3600 * 4, 3600 * 9))
        if kind == "fault":
            incident_cycle(arg)
        elif isinstance(arg, int):
            commit(*CLEAN_COMMITS[arg])
        else:
            files = ["requirements.txt"] if arg.startswith("build") else ["app/orders/routes.py"]
            commit("wip: " + arg.replace("_", " "), files, arg)

    # Bring the simulated environment back to the latest active deployment, healthy.
    env._now_fn = lambda: datetime.now(timezone.utc)  # type: ignore[attr-defined]
    env.clear_faults()
    with session_scope() as db:
        active = db.scalars(select(Deployment).where(Deployment.status == "active")
                            .order_by(Deployment.id.desc())).first()
        if active:
            env.deploy(active.version, active.image, active.commit_sha)


def seed_all(history: bool = True) -> None:
    seed_reference_data()
    if history:
        seed_history()


__all__ = ["seed_all", "MODULES"]
