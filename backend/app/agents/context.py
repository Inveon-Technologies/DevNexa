"""Execution context shared by agents: DB sessions, target environment, and clock.

Agents run inside LangGraph graphs; the context travels in ``config["configurable"]["ctx"]`` so the same
graphs serve the live dashboard (real clock, short UX delays) and offline experiments (simulated clock).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session


class RealClock:
    def __init__(self, delay: float):
        self.delay = delay

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def advance(self, seconds: float) -> None:
        # Real work happens in real time; the pause only makes progress visible on the dashboard.
        if self.delay > 0:
            time.sleep(self.delay)


class SimClock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


# Modelled durations (seconds) of each step, used by the simulated clock during experiments.
STEP_SECONDS = {
    "build": 95, "test_per_second_of_suite": 1.0, "release": 15, "deploy": 60,
    "collect_evidence": 20, "retrieve": 2, "diagnose": 25, "policy": 1, "verify": 60,
    "learn": 2, "alert_eval": 60, "scrape": 15,
}
ACTION_SECONDS = {
    "restart_container": 30, "restart_dependency": 30, "clear_cache": 5, "rerun_job": 120,
    "rollback_deployment": 90, "revert_config": 45, "increase_pool_size": 60, "apply_db_index": 180,
    "rotate_certificate": 120, "escalate": 0,
}


@dataclass
class RunContext:
    session_factory: Callable[[], Session]
    env: Any
    clock: Any
    rng_seed: int = 7
    observability_gap: float = 0.0
    approver: Callable[[Any], tuple[bool, float]] | None = None  # experiments: simulated human approval
    use_rag: bool = True
    use_feedback: bool = True
    schedule_monitoring: bool = True
    extras: dict = field(default_factory=dict)

    @contextmanager
    def session(self):
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def ctx_of(config) -> RunContext:
    return config["configurable"]["ctx"]
