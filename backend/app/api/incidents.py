from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.monitoring import OPEN_STATES
from ..agents.runner import start_incident
from ..db import get_db
from ..models import Alert, Diagnosis, Incident, KnowledgeEntry, RemediationAction
from ..services.analyzer import CAUSES
from ..services.audit import audit
from ..services.embeddings import get_embedder
from ..services.retrieval import search_similar
from .serialize import many, to_dict

router = APIRouter(prefix="/api", tags=["incidents"])


def incident_summary(i: Incident) -> dict:
    d = to_dict(i, exclude=("workflow",))
    last = i.diagnoses[-1] if i.diagnoses else None
    d["diagnosis"] = {"root_cause_id": last.root_cause_id, "title": CAUSES.get(last.root_cause_id, {}).get("title"),
                      "confidence": last.confidence} if last else None
    pending = [a for a in i.actions if a.status == "pending_approval"]
    d["pending_action"] = to_dict(pending[0]) if pending else None
    end = i.resolved_at
    d["mttr_s"] = round((end - i.detected_at).total_seconds(), 1) if end else None
    d["detection_s"] = round((i.detected_at - i.fault_started_at).total_seconds(), 1) if i.fault_started_at else None
    return d


@router.get("/incidents")
def list_incidents(status: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    q = select(Incident).order_by(Incident.id.desc()).limit(limit)
    if status == "open":
        q = q.where(Incident.status.in_(OPEN_STATES))
    elif status:
        q = q.where(Incident.status == status)
    return [incident_summary(i) for i in db.scalars(q)]


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    i = db.get(Incident, incident_id)
    if not i:
        raise HTTPException(404, "incident not found")
    d = incident_summary(i)
    d["workflow"] = i.workflow
    d["evidence"] = many(i.evidence)
    d["diagnoses"] = [{**to_dict(x), "title": CAUSES.get(x.root_cause_id, {}).get("title")} for x in i.diagnoses]
    d["actions"] = many(i.actions)
    d["health_checks"] = many(i.health_checks)
    d["alerts"] = many(db.scalars(select(Alert).where(Alert.incident_id == i.id)))
    d["knowledge"] = many(db.scalars(select(KnowledgeEntry).where(KnowledgeEntry.incident_id == i.id)),
                          exclude=("embedding",))
    return d


class ReanalyzeRequest(BaseModel):
    actor: str = "operator"
    reject_current: bool = True


@router.post("/incidents/{incident_id}/reanalyze")
def reanalyze(incident_id: int, req: ReanalyzeRequest, db: Session = Depends(get_db)):
    i = db.get(Incident, incident_id)
    if not i:
        raise HTTPException(404, "incident not found")
    excluded = [d.root_cause_id for d in i.diagnoses if d.outcome == "rejected"]
    if req.reject_current and i.diagnoses and i.diagnoses[-1].outcome == "pending":
        i.diagnoses[-1].outcome = "rejected"
        excluded.append(i.diagnoses[-1].root_cause_id)
    for a in i.actions:
        if a.status == "pending_approval":
            a.status, a.decided_by, a.decided_at = "rejected", req.actor, datetime.now(timezone.utc)
    i.status = "investigating"
    i.human_interventions += 1
    audit(db, req.actor, "incident.reanalyze", "incident", i.id, f"Re-analysis requested for incident #{i.id}")
    db.commit()
    start_incident(i.id, resume="retrieve", excluded=sorted(set(excluded)))
    return {"ok": True, "excluded": sorted(set(excluded))}


@router.post("/incidents/{incident_id}/close")
def close_incident(incident_id: int, actor: str = "operator", note: str = "", db: Session = Depends(get_db)):
    i = db.get(Incident, incident_id)
    if not i:
        raise HTTPException(404, "incident not found")
    i.status, i.resolved_at = "closed", datetime.now(timezone.utc)
    i.human_interventions += 1
    audit(db, actor, "incident.closed", "incident", i.id, note or f"Incident #{i.id} closed manually")
    db.commit()
    return incident_summary(i)


class Decision(BaseModel):
    actor: str = "operator"
    note: str = ""


@router.get("/actions")
def list_actions(status: str | None = None, db: Session = Depends(get_db)):
    q = select(RemediationAction).order_by(RemediationAction.id.desc()).limit(200)
    if status:
        q = q.where(RemediationAction.status == status)
    out = []
    for a in db.scalars(q):
        d = to_dict(a)
        diag = db.get(Diagnosis, a.diagnosis_id) if a.diagnosis_id else None
        inc = db.get(Incident, a.incident_id)
        d["incident"] = {"id": inc.id, "title": inc.title, "severity": inc.severity, "status": inc.status}
        d["diagnosis"] = {"root_cause": diag.root_cause, "confidence": diag.confidence,
                          "title": CAUSES.get(diag.root_cause_id, {}).get("title"),
                          "supporting_evidence": diag.supporting_evidence[:4],
                          "uncertainty": diag.uncertainty} if diag else None
        out.append(d)
    return out


def _decide(action_id: int, approve: bool, dec: Decision, db: Session):
    a = db.get(RemediationAction, action_id)
    if not a:
        raise HTTPException(404, "action not found")
    if a.status != "pending_approval":
        raise HTTPException(409, f"action is {a.status}, not pending approval")
    a.status = "approved" if approve else "rejected"
    a.decided_by, a.decided_at, a.decision_note = dec.actor, datetime.now(timezone.utc), dec.note or a.decision_note
    inc = db.get(Incident, a.incident_id)
    inc.human_interventions += 1
    inc.workflow = list(inc.workflow or []) + [{
        "ts": datetime.now(timezone.utc).isoformat(), "step": "approval", "status": "done",
        "detail": f"{a.title} {'approved' if approve else 'rejected'} by {dec.actor}" + (f": {dec.note}" if dec.note else "")}]
    if not approve:
        inc.status = "diagnosed"
    audit(db, dec.actor, "action.approved" if approve else "action.rejected", "remediation_action", a.id,
          f"{a.title} for incident #{inc.id} {'approved' if approve else 'rejected'}", note=dec.note)
    db.commit()
    if approve:
        start_incident(inc.id, resume="execute", action_id=a.id, diagnosis_id=a.diagnosis_id)
    return to_dict(a)


@router.post("/actions/{action_id}/approve")
def approve(action_id: int, dec: Decision, db: Session = Depends(get_db)):
    return _decide(action_id, True, dec, db)


@router.post("/actions/{action_id}/reject")
def reject(action_id: int, dec: Decision, db: Session = Depends(get_db)):
    return _decide(action_id, False, dec, db)


@router.get("/alerts")
def list_alerts(limit: int = 100, db: Session = Depends(get_db)):
    return many(db.scalars(select(Alert).order_by(Alert.id.desc()).limit(limit)))


@router.get("/knowledge")
def list_knowledge(db: Session = Depends(get_db)):
    return many(db.scalars(select(KnowledgeEntry).order_by(KnowledgeEntry.id.desc())), exclude=("embedding",))


@router.get("/knowledge/search")
def search_knowledge(q: str, k: int = 5, db: Session = Depends(get_db)):
    hits = search_similar(db, query_text=q, k=k, min_similarity=0.0)
    return {"query": q, "embedder": get_embedder().name,
            "results": [{**h.as_dict(), "symptoms": h.entry.symptoms[:600]} for h in hits]}


class KnowledgePatch(BaseModel):
    verified: bool
    actor: str = "operator"


@router.patch("/knowledge/{kid}")
def patch_knowledge(kid: int, body: KnowledgePatch, db: Session = Depends(get_db)):
    k = db.get(KnowledgeEntry, kid)
    if not k:
        raise HTTPException(404, "knowledge entry not found")
    k.verified = body.verified
    audit(db, body.actor, "knowledge.verified" if body.verified else "knowledge.unverified", "knowledge", kid,
          f"Knowledge #{kid} marked {'verified' if body.verified else 'unverified (excluded from retrieval)'}")
    db.commit()
    return to_dict(k, exclude=("embedding",))
