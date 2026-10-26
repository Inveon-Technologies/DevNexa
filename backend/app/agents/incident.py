"""Incident Agent: evidence -> similar-incident retrieval -> LLM diagnosis -> policy -> remediation ->
verification -> learning, as a LangGraph state machine with a re-analysis loop."""

from __future__ import annotations

import random
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from ..config import get_settings
from ..models import Alert, Deployment, Diagnosis, EvidenceItem, HealthCheck, Incident, RemediationAction
from ..services import policy
from ..services.analyzer import CAUSES, DiagnosisContext, get_analyzer
from ..services.audit import audit
from ..services.feedback import record_verified_incident
from ..services.retrieval import search_similar
from ..services.telemetry import live_evidence, simulated_evidence, symptom_text
from ..simulation.catalog import ACTIONS
from .context import ACTION_SECONDS, STEP_SECONDS, ctx_of


class IncidentState(TypedDict, total=False):
    incident_id: int
    resume: str | None
    symptoms: str
    similar: list[dict]
    excluded: list[str]
    diagnosis_id: int
    action_id: int
    route: str


def _log(incident: Incident, now, step: str, status: str, detail: str):
    incident.workflow = list(incident.workflow or []) + [
        {"ts": now.isoformat(), "step": step, "status": status, "detail": detail}]


def _evidence_dicts(incident: Incident) -> list[dict]:
    return [{"kind": e.kind, "source": e.source, "title": e.title, "content": e.content} for e in incident.evidence]


# ------------------------------------------------------------------ nodes
def collect_evidence(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        inc.status = "investigating"
        _log(inc, ctx.clock.now(), "collect_evidence", "running", "Gathering logs, metrics, traces, commits, deployments")
        db.commit()
        if ctx.env.mode == "live":
            items = live_evidence(db, inc)
        else:
            rng = random.Random(ctx.rng_seed * 100003 + inc.id)
            items = simulated_evidence(db, inc, ctx.env.active_faults(), rng, ctx.observability_gap, ctx.clock.now())
        ctx.clock.advance(STEP_SECONDS["collect_evidence"])
        for it in items:
            db.add(EvidenceItem(incident_id=inc.id, collected_at=ctx.clock.now(), **it))
        symptoms = symptom_text(inc, items)
        _log(inc, ctx.clock.now(), "collect_evidence", "done",
             f"{len(items)} evidence items from " + ", ".join(sorted({i['source'] for i in items})))
        audit(db, "incident-agent", "evidence.collected", "incident", inc.id, f"Collected {len(items)} evidence items")
    return {"symptoms": symptoms, "excluded": state.get("excluded", [])}


def retrieve(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        symptoms = state.get("symptoms") or symptom_text(inc, _evidence_dicts(inc))
        similar = []
        if ctx.use_rag:
            hits = search_similar(db, query_text=symptoms, exclude_incident_id=inc.id)
            for h in hits:
                h.entry.times_retrieved += 1
            similar = [h.as_dict() for h in hits]
            detail = (f"{len(similar)} similar verified incident(s), best similarity "
                      f"{similar[0]['similarity']:.2f}" if similar else "No similar verified incidents in memory")
        else:
            detail = "Retrieval disabled (ablation)"
        ctx.clock.advance(STEP_SECONDS["retrieve"])
        _log(inc, ctx.clock.now(), "retrieve_similar", "done", detail)
    return {"symptoms": symptoms, "similar": similar}


def diagnose(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        inc.attempts = (inc.attempts or 0) + 1
        analyzer = get_analyzer()
        res = analyzer.diagnose(DiagnosisContext(
            alert_name=inc.alert_name, service=inc.service, summary=inc.summary, evidence=_evidence_dicts(inc),
            similar=state.get("similar", []), excluded_causes=state.get("excluded", []), use_rag=ctx.use_rag))
        ctx.clock.advance(STEP_SECONDS["diagnose"])
        d = Diagnosis(incident_id=inc.id, attempt=inc.attempts, root_cause_id=res.root_cause_id,
                      root_cause=res.root_cause, affected_component=res.affected_component,
                      confidence=res.confidence, uncertainty=res.uncertainty, reasoning=res.reasoning,
                      supporting_evidence=res.supporting_evidence, alternatives=res.alternatives,
                      similar_incidents=state.get("similar", []), proposed_action=res.proposed_action,
                      provider=res.provider, used_rag=ctx.use_rag, created_at=ctx.clock.now())
        db.add(d)
        inc.status = "diagnosed"
        _log(inc, ctx.clock.now(), "diagnose", "done",
             f"Attempt {inc.attempts}: {CAUSES[res.root_cause_id]['title']} (confidence {res.confidence:.2f}, {res.provider})")
        db.flush()
        audit(db, "incident-agent", "diagnosis.created", "incident", inc.id,
              f"Diagnosis: {CAUSES[res.root_cause_id]['title']} -> {res.proposed_action}", confidence=res.confidence)
        return {"diagnosis_id": d.id}


def _rollback_params(db) -> dict:
    current = db.scalars(select(Deployment).where(Deployment.status == "active").order_by(Deployment.id.desc())).first()
    target = db.scalars(select(Deployment).where(Deployment.is_stable.is_(True),
                                                 Deployment.status.in_(["superseded", "active"]),
                                                 Deployment.id != (current.id if current else -1))
                        .order_by(Deployment.id.desc())).first()
    if not target:
        return {}
    return {"version": target.version, "image": target.image, "commit_sha": target.commit_sha,
            "target_deployment_id": target.id, "current_deployment_id": current.id if current else None}


def decide(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        d = db.get(Diagnosis, state["diagnosis_id"])
        spec = ACTIONS[d.proposed_action]
        pd = policy.evaluate(db, spec.type, d.confidence)
        params = _rollback_params(db) if spec.type == "rollback_deployment" else {}
        desc = spec.description
        if params:
            desc += f" Target: {params['version']} ({params['image']})."
        act = RemediationAction(incident_id=inc.id, diagnosis_id=d.id, action_type=spec.type, title=spec.title,
                                description=desc, params=params, risk_level=pd.risk_level, decision=pd.decision,
                                requested_at=ctx.clock.now(), decision_note=pd.reason)
        db.add(act)
        db.flush()
        ctx.clock.advance(STEP_SECONDS["policy"])
        route = "execute"
        if pd.decision == "blocked":
            act.status, route = "rejected", "escalate"
        elif pd.decision == "auto_execute":
            act.status, act.decided_by, act.decided_at = "approved", "policy", ctx.clock.now()
        else:
            act.status = "pending_approval"
            inc.status = "awaiting_approval"
            route = "wait"
            if ctx.approver is not None:
                approved, latency = ctx.approver(act)
                ctx.clock.advance(latency)
                inc.human_interventions += 1
                act.decided_by, act.decided_at = "experiment-approver", ctx.clock.now()
                act.status = "approved" if approved else "rejected"
                route = "execute" if approved else "escalate"
        _log(inc, ctx.clock.now(), "policy", "done", f"{spec.title}: {pd.risk_level} risk -> {pd.decision}. {pd.reason}")
        audit(db, "policy-engine", f"action.{pd.decision}", "remediation_action", act.id,
              f"{spec.title} ({pd.risk_level} risk) for incident #{inc.id}: {pd.decision}")
        return {"action_id": act.id, "route": route}


def execute(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        act = db.get(RemediationAction, state["action_id"])
        if act.action_type == "escalate":
            act.status, act.executed_at = "executed", ctx.clock.now()
            act.result = {"ok": True, "output": "Escalated to on-call engineer"}
            return {"route": "escalate"}
        inc.status = "remediating"
        _log(inc, ctx.clock.now(), "execute", "running", f"Executing {act.title}")
        db.commit()
        try:
            result = ctx.env.execute_action(act.action_type, act.params)
            ok = bool(result.get("ok", True))
        except Exception as e:  # executor failure is recorded, never reported as success
            result, ok = {"ok": False, "error": str(e)}, False
        ctx.clock.advance(ACTION_SECONDS.get(act.action_type, 30))
        act.result, act.executed_at = result, ctx.clock.now()
        act.status = "executed" if ok else "failed"
        if ok and act.action_type == "rollback_deployment" and act.params.get("target_deployment_id"):
            cur = db.get(Deployment, act.params.get("current_deployment_id") or -1)
            tgt = db.get(Deployment, act.params["target_deployment_id"])
            if cur:
                cur.status, cur.health, cur.is_stable = "rolled_back", "unhealthy", False
            tgt.status, tgt.health = "active", "healthy"
        _log(inc, ctx.clock.now(), "execute", "done" if ok else "failed",
             result.get("output") or result.get("error") or str(result))
        audit(db, act.decided_by or "executor", "action.executed" if ok else "action.failed", "remediation_action",
              act.id, f"{act.title}: {'ok' if ok else 'failed'}")
    return {"route": "verify"}


def verify(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    s = get_settings()
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        act = db.get(RemediationAction, state["action_id"])
        d = db.get(Diagnosis, state["diagnosis_id"])
        inc.status = "verifying"
        db.commit()
        checks = ctx.env.health()
        if ctx.env.mode == "live":  # pragma: no cover - give rate() windows time to recover
            for _ in range(9):
                if all(c["passed"] for c in checks):
                    break
                time.sleep(10)
                checks = ctx.env.health()
        ctx.clock.advance(STEP_SECONDS["verify"])
        passed = act.status == "executed" and all(c["passed"] for c in checks)
        db.add(HealthCheck(incident_id=inc.id, action_id=act.id, target=inc.service, passed=passed, checks=checks,
                           checked_at=ctx.clock.now()))
        failing = [c["name"] for c in checks if not c["passed"]]
        _log(inc, ctx.clock.now(), "verify", "done" if passed else "failed",
             "All health checks passed" if passed else "Still failing: " + ", ".join(failing or ["action failed"]))
        if passed:
            act.status, d.outcome = "verified", "confirmed"
            return {"route": "learn"}
        act.status = "verification_failed" if act.status == "executed" else act.status
        d.outcome = "rejected"
        excluded = list(state.get("excluded", [])) + [d.root_cause_id]
        audit(db, "verifier", "verification.failed", "incident", inc.id,
              f"Verification failed after {act.title}; " + ("re-analysing" if inc.attempts <= s.max_reanalysis_attempts
                                                            else "escalating"))
        if inc.attempts <= s.max_reanalysis_attempts:
            return {"route": "reanalyze", "excluded": excluded}
        return {"route": "escalate", "excluded": excluded}


def learn(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        act = db.get(RemediationAction, state["action_id"])
        d = db.get(Diagnosis, state["diagnosis_id"])
        now = ctx.clock.now()
        inc.status, inc.resolved_at, inc.confirmed_cause_id = "resolved", now, d.root_cause_id
        mttr = (now - inc.detected_at).total_seconds()
        for a in db.scalars(select(Alert).where(Alert.incident_id == inc.id)):
            a.status = "resolved"
        if ctx.use_feedback and d.root_cause_id != "unknown":
            k = record_verified_incident(db, inc, d, act, state.get("symptoms") or symptom_text(inc, _evidence_dicts(inc)),
                                         mttr)
            ctx.clock.advance(STEP_SECONDS["learn"])
            _log(inc, ctx.clock.now(), "learn", "done",
                 f"Stored verified knowledge #{k.id}; regression test {k.regression_test} added; module "
                 f"{k.affected_module} risk updated")
            audit(db, "feedback-engine", "knowledge.stored", "knowledge", k.id,
                  f"Verified incident #{inc.id} stored for future retrieval and test prioritisation")
        else:
            _log(inc, now, "learn", "skipped", "Feedback Engine disabled")
        audit(db, "incident-agent", "incident.resolved", "incident", inc.id,
              f"Incident #{inc.id} resolved and verified in {mttr / 60:.1f} min")
    return {}


def escalate(state: IncidentState, config) -> IncidentState:
    ctx = ctx_of(config)
    with ctx.session() as db:
        inc = db.get(Incident, state["incident_id"])
        inc.status = "escalated"
        inc.human_interventions += 1
        _log(inc, ctx.clock.now(), "escalate", "done", "Handed to on-call engineer with the evidence bundle")
        audit(db, "incident-agent", "incident.escalated", "incident", inc.id, f"Incident #{inc.id} escalated")
    return {}


def build_incident_graph():
    g = StateGraph(IncidentState)
    for name, fn in [("collect_evidence", collect_evidence), ("retrieve", retrieve), ("diagnose", diagnose),
                     ("decide", decide), ("execute", execute), ("verify", verify), ("learn", learn),
                     ("escalate", escalate)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, lambda s: s.get("resume") or "collect_evidence",
                            {"collect_evidence": "collect_evidence", "retrieve": "retrieve", "execute": "execute"})
    g.add_edge("collect_evidence", "retrieve")
    g.add_edge("retrieve", "diagnose")
    g.add_edge("diagnose", "decide")
    g.add_conditional_edges("decide", lambda s: s["route"], {"execute": "execute", "wait": END, "escalate": "escalate"})
    g.add_conditional_edges("execute", lambda s: s["route"], {"verify": "verify", "escalate": "escalate"})
    g.add_conditional_edges("verify", lambda s: s["route"],
                            {"learn": "learn", "reanalyze": "retrieve", "escalate": "escalate"})
    g.add_edge("learn", END)
    g.add_edge("escalate", END)
    return g.compile()


INCIDENT_GRAPH = build_incident_graph()


def run_incident(ctx, incident_id: int, resume: str | None = None, **state) -> dict:
    return INCIDENT_GRAPH.invoke({"incident_id": incident_id, "resume": resume, **state},
                                 config={"configurable": {"ctx": ctx}, "recursion_limit": 60})
