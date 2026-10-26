"""Controlled fault-injection experiments (synopsis §8.5).

Each variant replays the same randomised sequence of fault occurrences (every scenario repeated N times,
interleaved with clean commits) against a fresh simulated environment and a fresh in-memory database:

* ``devnexa``  - full platform: prioritised tests, LLM+RAG diagnosis, graded autonomy, Feedback Engine
* ``no_rag``   - ablation: same platform with similar-incident retrieval switched off
* ``baseline`` - conventional CI/CD: full test suite, no Feedback Engine, manual monitoring and diagnosis

DevNexa variants are *measured* by executing the real agent graphs on a simulated clock. The conventional
baseline's human steps cannot be executed, so they are modelled with the explicit, editable assumptions in
``DEFAULT_CONFIG["baseline"]``; the report labels them as assumptions.
"""

from __future__ import annotations

import random
import statistics
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ..agents.cicd import create_run, run_pipeline
from ..agents.context import RunContext, SimClock, STEP_SECONDS
from ..agents.incident import run_incident
from ..agents.monitoring import scan
from ..agents.runner import fake_sha
from ..db import Base
from ..models import Deployment, Diagnosis, Incident, PipelineRun
from ..seed import CLEAN_COMMITS
from ..services.environment import DEFAULT_STATE, SimulatedEnvironment
from ..services.feedback import seed_test_suite
from ..services.policy import seed_policies, set_autonomy
from ..simulation.catalog import FAULTS, SERVICE, SHIP_VIA_PIPELINE

DEFAULT_CONFIG = {
    "scenarios": list(FAULTS),
    "repetitions": 3,
    "seed": 2026,
    "observability_gap": 0.6,          # probability that each distinctive log line is missing
    "approval_latency_s": [120, 600],  # simulated human approval delay (uniform range)
    "escalation_resolution_s": [1800, 3600],
    "baseline": {                      # assumptions for the conventional workflow
        "detection_s": [300, 1200],
        "diagnosis_s": [900, 2700],
        "fix_s": [300, 900],
        "manual_verification_s": [300, 600],
        "rca_accuracy": 0.7,
        "retry_diagnosis_s": [900, 1800],
        "manual_steps": 4,
    },
}

VARIANTS = {
    "devnexa": {"label": "DevNexa (LLM+RAG + feedback)", "use_rag": True, "use_feedback": True,
                "strategy": "prioritized"},
    "no_rag": {"label": "DevNexa without RAG", "use_rag": False, "use_feedback": True, "strategy": "prioritized"},
    "baseline": {"label": "Conventional CI/CD", "use_rag": False, "use_feedback": False, "strategy": "full"},
}


def _fresh_world(clock: SimClock):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    env = SimulatedEnvironment(DEFAULT_STATE, now_fn=clock.now)
    with factory() as db:
        seed_policies(db)
        seed_test_suite(db)
        set_autonomy(db, {"auto_execute_low": True})
        st = DEFAULT_STATE
        db.add(Deployment(service=SERVICE, version=st["version"], image=st["image"], commit_sha=st["commit_sha"],
                          status="active", health="healthy", is_stable=True, deployed_at=clock.now()))
        db.commit()
    return engine, factory, env


def _sequence(cfg) -> list[str]:
    rng = random.Random(cfg["seed"])
    seq = [s for s in cfg["scenarios"] for _ in range(cfg["repetitions"])]
    rng.shuffle(seq)
    return seq


def run_variant(name: str, cfg: dict) -> dict:
    v = VARIANTS[name]
    rng = random.Random(cfg["seed"] * 7 + len(name))
    clock = SimClock(datetime(2026, 3, 1, tzinfo=timezone.utc))
    engine, factory, env = _fresh_world(clock)
    lat = cfg["approval_latency_s"]
    ctx = RunContext(session_factory=factory, env=env, clock=clock, rng_seed=cfg["seed"],
                     observability_gap=cfg["observability_gap"], use_rag=v["use_rag"],
                     use_feedback=v["use_feedback"], approver=lambda _a: (True, rng.uniform(*lat)))
    b = cfg["baseline"]
    seen: set[str] = set()
    records, pipelines = [], []

    def commit(message, files, defect=None):
        with ctx.session() as db:
            run = create_run(db, commit_sha=fake_sha(f"{name}{message}{clock.now()}"), message=message, files=files,
                             author="experiment", trigger="experiment", strategy=v["strategy"], defect=defect,
                             now=clock.now())
            rid = run.id
        state = run_pipeline(ctx, rid)
        with ctx.session() as db:
            run = db.get(PipelineRun, rid)
            test_stage = next(s for s in run.stages if s.name == "test")
            d = test_stage.details or {}
            pipelines.append({"run_id": rid, "defect": defect, "failed_stage": state.get("failed_stage"),
                              "tests_run": d.get("selected", 0), "test_time_s": d.get("duration_s", 0.0),
                              "full_suite_s": d.get("full_suite_s", 0.0),
                              "caught": bool(defect) and state.get("failed_stage") == "test"})
        return state

    for i, fault in enumerate(_sequence(cfg)):
        clock.advance(3600)
        cf = CLEAN_COMMITS[i % len(CLEAN_COMMITS)]
        commit(*cf)
        repeat = fault in seen
        seen.add(fault)
        rec = {"index": i, "scenario": fault, "repeat": repeat, "prevented_in_ci": False, "incident": False}
        if fault in SHIP_VIA_PIPELINE:
            state = commit(FAULTS[fault].commit["message"], FAULTS[fault].commit["files"], fault)
            if state.get("failed_stage"):
                rec["prevented_in_ci"] = True
                records.append(rec)
                continue
        else:
            env.inject_fault(fault)
        fault_start = clock.now()
        rec["incident"] = True

        if name == "baseline":
            detection = rng.uniform(*b["detection_s"])
            correct = rng.random() < b["rca_accuracy"]
            mttr = rng.uniform(*b["diagnosis_s"]) + rng.uniform(*b["fix_s"]) + rng.uniform(*b["manual_verification_s"])
            if not correct:
                mttr += rng.uniform(*b["retry_diagnosis_s"]) + rng.uniform(*b["fix_s"])
            rec.update(detection_s=detection, rca_correct=correct, mttr_s=mttr, human_steps=b["manual_steps"]
                       + (0 if correct else 2), resolved_auto=False, escalated=False, attempts=1 if correct else 2)
            clock.advance(detection + mttr)
            env.clear_faults()
            records.append(rec)
            continue

        clock.advance(STEP_SECONDS["scrape"] + STEP_SECONDS["alert_eval"])
        inc_id = scan(ctx)
        if inc_id is None:
            rec["incident"] = False
            env.clear_faults()
            records.append(rec)
            continue
        run_incident(ctx, inc_id)
        with ctx.session() as db:
            inc = db.get(Incident, inc_id)
            first = db.scalars(select(Diagnosis).where(Diagnosis.incident_id == inc_id)
                               .order_by(Diagnosis.attempt)).first()
            detection = (inc.detected_at - fault_start).total_seconds()
            if inc.status == "resolved":
                mttr = (inc.resolved_at - inc.detected_at).total_seconds()
            else:
                mttr = (clock.now() - inc.detected_at).total_seconds() + rng.uniform(*cfg["escalation_resolution_s"])
                inc.status, inc.resolved_at = "resolved", clock.now()
            rec.update(detection_s=detection, rca_correct=bool(first and first.root_cause_id == fault),
                       diagnosed_as=first.root_cause_id if first else None,
                       confidence=first.confidence if first else None, mttr_s=mttr,
                       human_steps=inc.human_interventions, attempts=inc.attempts,
                       escalated=inc.human_interventions > 0 and inc.status == "escalated",
                       resolved_auto=inc.human_interventions == 0, incident_id=inc_id,
                       similar_found=len(first.similar_incidents) if first else 0)
        env.clear_faults()
        records.append(rec)

    engine.dispose()
    return {"label": v["label"], "records": records, "pipelines": pipelines, "metrics": _metrics(records, pipelines)}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 2) if xs else None


def _metrics(records: list[dict], pipelines: list[dict]) -> dict:
    inc = [r for r in records if r["incident"]]
    defect_runs = [p for p in pipelines if p["defect"]]
    test_time = sum(p["test_time_s"] for p in pipelines)
    caught = sum(1 for p in pipelines if p["caught"])
    return {
        "fault_occurrences": len(records),
        "incidents": len(inc),
        "prevented_in_ci": sum(1 for r in records if r["prevented_in_ci"]),
        "repeated_incidents": sum(1 for r in inc if r["repeat"]),
        "failure_detection_s": _mean([r["detection_s"] for r in inc]),
        "rca_accuracy": _mean([1.0 if r["rca_correct"] else 0.0 for r in inc]),
        "mttr_s": _mean([r["mttr_s"] for r in inc]),
        "human_steps_per_incident": _mean([r["human_steps"] for r in inc]),
        "auto_resolved": sum(1 for r in inc if r.get("resolved_auto")),
        "pipeline_runs": len(pipelines),
        "avg_tests_per_run": _mean([p["tests_run"] for p in pipelines]),
        "avg_test_time_s": _mean([p["test_time_s"] for p in pipelines]),
        "test_time_saved_pct": round(100 * (1 - test_time / max(1e-9, sum(p["full_suite_s"] for p in pipelines))), 1)
        if pipelines else None,
        "faults_caught_in_ci": caught,
        "faulty_builds": len(defect_runs),
        "faults_per_test_hour": round(caught / (test_time / 3600), 2) if test_time else 0.0,
    }


def run_experiment(cfg: dict | None = None, variants: list[str] | None = None) -> dict:
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    cfg["baseline"] = {**DEFAULT_CONFIG["baseline"], **(cfg.get("baseline") or {})}
    results = {name: run_variant(name, cfg) for name in (variants or list(VARIANTS))}
    by_scenario = {}
    for name, res in results.items():
        for r in res["records"]:
            s = by_scenario.setdefault(r["scenario"], {}).setdefault(name, {"incidents": 0, "correct": 0, "mttr": []})
            if r["incident"]:
                s["incidents"] += 1
                s["correct"] += 1 if r["rca_correct"] else 0
                s["mttr"].append(r["mttr_s"])
    scenario_table = [
        {"scenario": sc, "title": FAULTS[sc].title,
         **{f"{n}_rca": (round(v["correct"] / v["incidents"], 2) if v["incidents"] else None) for n, v in d.items()},
         **{f"{n}_mttr_s": _mean(v["mttr"]) for n, v in d.items()},
         **{f"{n}_incidents": v["incidents"] for n, v in d.items()}}
        for sc, d in sorted(by_scenario.items())
    ]
    return {
        "config": cfg,
        "variants": {n: {"label": r["label"], "metrics": r["metrics"]} for n, r in results.items()},
        "scenarios": scenario_table,
        "records": {n: r["records"] for n, r in results.items()},
        "notes": [
            "DevNexa and no-RAG variants are measured by executing the agent workflows against the simulated "
            "environment on a simulated clock.",
            "Conventional-baseline human timings and diagnosis accuracy are modelled assumptions (see config.baseline).",
            f"Observability gap {cfg['observability_gap']:.0%}: each distinctive log line is dropped with this probability.",
        ],
    }
