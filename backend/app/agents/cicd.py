"""Build, Test, Release and Deployment agents, coordinated by a LangGraph workflow."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from ..models import Deployment, PipelineRun, StageRun, TestCase, TestExecution
from ..services.analyzer import explain_ci_failure
from ..services.audit import audit
from ..services.feedback import changed_modules, pipeline_risk, prioritize_tests
from ..simulation.catalog import CI_DEFECTS, FAULTS, SERVICE
from .context import STEP_SECONDS, RunContext, ctx_of

STAGES = [("build", "Build Agent"), ("test", "Test Agent"), ("release", "Release Agent"),
          ("deploy", "Deployment Agent")]
RELEASE_RISK_GATE = 0.85


class PipelineState(TypedDict, total=False):
    run_id: int
    failed_stage: str | None
    deployment_id: int | None


def _stage(db, run: PipelineRun, name: str) -> StageRun:
    return next(s for s in run.stages if s.name == name)


def _start(ctx: RunContext, db, run: PipelineRun, name: str) -> StageRun:
    st = _stage(db, run, name)
    st.status, st.started_at = "running", ctx.clock.now()
    run.current_stage = name
    db.commit()
    return st


def _finish(ctx: RunContext, st: StageRun, ok: bool, logs: list[str], **details):
    st.status = "success" if ok else "failed"
    st.finished_at = ctx.clock.now()
    st.logs = "\n".join(logs)
    st.details = {**(st.details or {}), **details}


def _fail_run(ctx: RunContext, db, run: PipelineRun, stage: str, log: str):
    fb = explain_ci_failure(stage, log, run.changed_files)
    run.status = "failed"
    run.finished_at = ctx.clock.now()
    run.ai_feedback = f"{fb['summary']}\n\nSuggested fix: {fb['suggestion']}"
    for s in run.stages:
        if s.status == "pending":
            s.status = "skipped"
    audit(db, "build-agent" if stage == "build" else "test-agent", "pipeline.failed", "pipeline_run", run.id,
          f"Pipeline #{run.id} failed at {stage}; AI feedback sent to {run.author}", provider=fb["provider"])


# ------------------------------------------------------------------ nodes
def build_node(state: PipelineState, config) -> PipelineState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        run = db.get(PipelineRun, state["run_id"])
        run.status, run.started_at = "running", run.started_at or ctx.clock.now()
        st = _start(ctx, db, run, "build")
        ctx.clock.advance(STEP_SECONDS["build"])
        logs = [f"$ git checkout {run.commit_sha[:7]}  ({run.branch})",
                "$ pip install -r requirements.txt"]
        defect = CI_DEFECTS.get(run.injected_defect or "")
        if defect and defect.stage == "build":
            logs += defect.log.splitlines()
            _finish(ctx, st, False, logs, defect=defect.id)
            _fail_run(ctx, db, run, "build", defect.log)
            return {"failed_stage": "build"}
        logs += ["Successfully installed 38 packages", "$ python -m compileall -q app",
                 f"$ docker build -t devnexa/sample-app:{run.commit_sha[:7]} .",
                 "Step 9/9 : CMD [\"uvicorn\", \"app.main:app\"]", f"Successfully tagged devnexa/sample-app:{run.commit_sha[:7]}"]
        _finish(ctx, st, True, logs, image=f"devnexa/sample-app:{run.commit_sha[:7]}",
                modules=changed_modules(run.changed_files))
        audit(db, "build-agent", "build.succeeded", "pipeline_run", run.id, f"Build passed for {run.commit_sha[:7]}")
    return {"failed_stage": None}


def test_node(state: PipelineState, config) -> PipelineState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        run = db.get(PipelineRun, state["run_id"])
        st = _start(ctx, db, run, "test")
        if run.test_strategy == "full":
            ranked = [{"test": t, "score": 0.0, "reasons": ["full suite"], "selected": True}
                      for t in db.scalars(select(TestCase).order_by(TestCase.module, TestCase.id))]
        else:
            ranked = prioritize_tests(db, run.changed_files)
        failures, total_time, logs = [], 0.0, [f"Test strategy: {run.test_strategy}"]
        defect = run.injected_defect
        for i, r in enumerate(ranked):
            t: TestCase = r["test"]
            ex = TestExecution(run_id=run.id, test_id=t.id, module=t.module, order=i, selected=r["selected"],
                               priority_score=r["score"], reasons=r["reasons"])
            if r["selected"]:
                failed = bool(defect) and defect in (t.detects or [])
                ex.status = "failed" if failed else "passed"
                ex.duration_s = t.duration_s
                total_time += t.duration_s
                if failed:
                    t.failure_count += 1
                    msg = CI_DEFECTS[defect].log if defect in CI_DEFECTS else (
                        f"AssertionError: regression for verified incident cause '{defect}' reproduced "
                        f"({FAULTS[defect].title})" if defect in FAULTS else "AssertionError")
                    ex.message = msg
                    failures.append((t.id, msg))
                logs.append(f"{'FAILED' if failed else 'PASSED'} {t.id} ({t.duration_s:.1f}s)")
            else:
                ex.status = "skipped"
            db.add(ex)
        ctx.clock.advance(total_time * STEP_SECONDS["test_per_second_of_suite"])
        selected = sum(1 for r in ranked if r["selected"])
        summary = dict(selected=selected, total=len(ranked), duration_s=round(total_time, 1),
                       full_suite_s=round(sum(r["test"].duration_s for r in ranked), 1), failed=len(failures))
        logs.append(f"{selected}/{len(ranked)} tests run in {total_time:.1f}s, {len(failures)} failed")
        if failures:
            logs += [f"\n{tid}\n{msg}" for tid, msg in failures]
            _finish(ctx, st, False, logs, **summary)
            _fail_run(ctx, db, run, "test", "\n".join(f"FAILED {tid} - {msg}" for tid, msg in failures))
            if defect in FAULTS:
                run.ai_feedback = (
                    f"Regression test {failures[0][0]} (added by the Feedback Engine after a verified "
                    f"incident) caught a reintroduced defect: {FAULTS[defect].root_cause}\n\n"
                    f"Suggested fix: apply the verified remediation for '{FAULTS[defect].title}' before merging."
                )
            return {"failed_stage": "test"}
        _finish(ctx, st, True, logs, **summary)
        audit(db, "test-agent", "tests.passed", "pipeline_run", run.id, logs[-1])
    return {"failed_stage": None}


def release_node(state: PipelineState, config) -> PipelineState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        run = db.get(PipelineRun, state["run_id"])
        st = _start(ctx, db, run, "release")
        ctx.clock.advance(STEP_SECONDS["release"])
        test_ok = _stage(db, run, "test").status == "success"
        checks = [
            {"name": "All selected tests passed", "passed": test_ok},
            {"name": "Image built and tagged", "passed": True},
            {"name": f"Change risk below gate ({RELEASE_RISK_GATE})", "passed": run.risk_score < RELEASE_RISK_GATE,
             "value": run.risk_score},
        ]
        ok = all(c["passed"] for c in checks)
        logs = [f"{'✔' if c['passed'] else '✘'} {c['name']}" for c in checks]
        if run.risk_factors:
            logs += ["Risk factors:"] + [f"  - {f}" for f in run.risk_factors]
        _finish(ctx, st, ok, logs, checks=checks)
        if not ok:
            run.status, run.finished_at = "failed", ctx.clock.now()
            run.ai_feedback = ("Release blocked: the change touches modules with a history of verified "
                               "incidents. Ask a reviewer to approve or add targeted tests.\n\n"
                               + "\n".join(run.risk_factors))
            for s in run.stages:
                if s.status == "pending":
                    s.status = "skipped"
            return {"failed_stage": "release"}
        audit(db, "release-agent", "release.ready", "pipeline_run", run.id, "Release readiness checks passed")
    return {"failed_stage": None}


def deploy_node(state: PipelineState, config) -> PipelineState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        run = db.get(PipelineRun, state["run_id"])
        st = _start(ctx, db, run, "deploy")
        prev = db.scalars(select(Deployment).where(Deployment.status == "active")
                          .order_by(Deployment.id.desc())).first()
        n = db.query(Deployment).count()
        version = f"v1.4.{n + 1}"
        image = f"devnexa/sample-app:{run.commit_sha[:7]}"
        ctx.env.deploy(version, image, run.commit_sha, run.injected_defect)
        ctx.clock.advance(STEP_SECONDS["deploy"])
        checks = ctx.env.health()
        live = checks[0]["passed"]
        dep = Deployment(run_id=run.id, service=SERVICE, version=version, image=image, commit_sha=run.commit_sha,
                         status="active" if live else "failed", health="healthy" if live else "unhealthy",
                         is_stable=False, deployed_at=ctx.clock.now())
        db.add(dep)
        db.flush()
        logs = [f"$ docker compose up -d sample-app  # {image}", f"Deployed {version} to staging",
                f"Post-deploy liveness: {'up' if live else 'DOWN'}"]
        if live:
            if prev:
                prev.status = "superseded"
                prev.is_stable = prev.health == "healthy"
            _finish(ctx, st, True, logs, deployment_id=dep.id, version=version)
            run.status, run.finished_at, run.current_stage = "success", ctx.clock.now(), None
            audit(db, "deployment-agent", "deploy.succeeded", "deployment", dep.id, f"{version} deployed ({image})")
        else:
            # Automatic rollback when the new release does not even come up.
            if prev:
                ctx.env.rollback(prev.version, prev.image, prev.commit_sha)
                logs.append(f"Liveness failed: rolled back to {prev.version}")
            dep.status = "rolled_back"
            _finish(ctx, st, False, logs, deployment_id=dep.id)
            _fail_run(ctx, db, run, "deploy", "\n".join(logs))
            run.ai_feedback = (f"Deployment {version} failed its liveness check and was rolled back "
                               f"automatically to {prev.version if prev else 'the previous release'}.")
            return {"failed_stage": "deploy", "deployment_id": dep.id}
    return {"failed_stage": None, "deployment_id": dep.id}


def _after(state: PipelineState) -> str:
    return "stop" if state.get("failed_stage") else "next"


def build_pipeline_graph():
    g = StateGraph(PipelineState)
    g.add_node("build", build_node)
    g.add_node("test", test_node)
    g.add_node("release", release_node)
    g.add_node("deploy", deploy_node)
    g.add_edge(START, "build")
    g.add_conditional_edges("build", _after, {"next": "test", "stop": END})
    g.add_conditional_edges("test", _after, {"next": "release", "stop": END})
    g.add_conditional_edges("release", _after, {"next": "deploy", "stop": END})
    g.add_edge("deploy", END)
    return g.compile()


PIPELINE_GRAPH = build_pipeline_graph()


def create_run(db, *, commit_sha: str, message: str, files: list[str], author: str = "developer",
               branch: str = "main", trigger: str = "manual", strategy: str = "prioritized",
               defect: str | None = None, repo: str = "devnexa/sample-app", now=None) -> PipelineRun:
    risk, factors = pipeline_risk(db, files)
    run = PipelineRun(repo=repo, branch=branch, commit_sha=commit_sha, commit_message=message, author=author,
                      changed_files=files, trigger=trigger, test_strategy=strategy, injected_defect=defect,
                      risk_score=risk, risk_factors=factors)
    if now is not None:
        run.created_at = now
    run.stages = [StageRun(name=n, agent=a, status="pending") for n, a in STAGES]
    db.add(run)
    db.flush()
    audit(db, trigger, "pipeline.triggered", "pipeline_run", run.id,
          f"Pipeline #{run.id} triggered by {trigger} for {commit_sha[:7]} '{message}'")
    return run


def run_pipeline(ctx: RunContext, run_id: int) -> PipelineState:
    return PIPELINE_GRAPH.invoke({"run_id": run_id}, config={"configurable": {"ctx": ctx}})
