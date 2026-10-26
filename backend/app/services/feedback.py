"""Feedback Engine: turn verified incidents into reusable knowledge, module risk and regression tests."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Incident, KnowledgeEntry, ModuleRisk, TestCase
from ..simulation.catalog import FAULTS, TEST_SUITE, module_for_file
from .analyzer import CAUSES
from .embeddings import get_embedder

RISK_PER_INCIDENT = 0.35


def seed_test_suite(db: Session) -> None:
    for t in TEST_SUITE:
        if not db.get(TestCase, t.id):
            db.add(TestCase(id=t.id, name=t.id.split("::")[-1], module=t.module, covers=list(t.covers),
                            detects=list(t.detects), duration_s=t.duration_s, kind=t.kind))
    db.flush()


def regression_test_for(cause_id: str, module: str) -> str:
    if cause_id in FAULTS:
        return FAULTS[cause_id].missed_test
    slug = module.replace("/", "_")
    return f"tests/regression/test_{slug}.py::test_{cause_id}_does_not_recur"


def record_verified_incident(db: Session, incident: Incident, diagnosis, action, symptoms: str,
                             mttr_s: float | None) -> KnowledgeEntry:
    """Store verified knowledge. Only called after post-action health verification passed."""
    cause = diagnosis.root_cause_id
    module = CAUSES.get(cause, {}).get("module", "unknown")
    reg_test = regression_test_for(cause, module) if cause != "unknown" else None
    entry = KnowledgeEntry(
        incident_id=incident.id,
        title=f"{incident.alert_name}: {CAUSES.get(cause, {'title': cause})['title']}",
        symptoms=symptoms[:4000],
        root_cause_id=cause,
        root_cause=diagnosis.root_cause,
        action_type=action.action_type,
        outcome="resolved",
        service=incident.service,
        affected_module=module,
        missed_test=reg_test,
        regression_test=reg_test,
        tags=list(FAULTS[cause].tags) if cause in FAULTS else [],
        verified=True,
        mttr_s=mttr_s,
        embedding=get_embedder().embed(symptoms),
    )
    db.add(entry)

    if module != "unknown":
        risk = db.get(ModuleRisk, module) or ModuleRisk(module=module, risk_score=0.0, incident_count=0)
        risk.incident_count += 1
        risk.risk_score = min(1.0, risk.risk_score + RISK_PER_INCIDENT * (1 - risk.risk_score) + 0.05)
        risk.last_incident_at = datetime.now(timezone.utc)
        db.add(risk)

    if reg_test and not db.get(TestCase, reg_test):
        db.add(TestCase(id=reg_test, name=reg_test.split("::")[-1], module=module, covers=[module],
                        detects=[cause], duration_s=1.2, kind="regression", is_regression=True,
                        origin_incident_id=incident.id))
    db.flush()
    return entry


def changed_modules(files: list[str]) -> list[str]:
    return sorted({m for f in files if (m := module_for_file(f))})


def prioritize_tests(db: Session, files: list[str], budget_ratio: float = 0.5) -> list[dict]:
    """Rank tests by change impact, module risk learned from incidents, and failure history.

    Selected = smoke tests + regression tests for touched/risky modules + everything scoring above the cut,
    capped at roughly ``budget_ratio`` of total suite time (never dropping a positive-impact test).
    """
    mods = set(changed_modules(files))
    risks = {r.module: r.risk_score for r in db.scalars(select(ModuleRisk))}
    tests = db.scalars(select(TestCase)).all()
    scored = []
    for t in tests:
        reasons, score = [], 0.0
        covers = set(t.covers or [t.module])
        touched = covers & mods
        if t.module in mods:
            score += 1.0
            reasons.append("tests a changed module")
        elif touched:
            score += 0.6
            reasons.append("covers a changed module")
        risk = max((risks.get(m, 0.0) for m in covers), default=0.0)
        if risk:
            score += 0.8 * risk
            reasons.append(f"module risk {risk:.2f} from verified incidents")
        if t.is_regression:
            score += 0.7 if touched or risk else 0.2
            reasons.append(f"regression test from incident #{t.origin_incident_id}")
        if t.failure_count:
            score += min(0.5, 0.1 * t.failure_count)
            reasons.append(f"failed {t.failure_count}x before")
        if t.kind == "smoke":
            score += 0.5
            reasons.append("smoke test")
        score -= 0.02 * t.duration_s
        scored.append({"test": t, "score": round(score, 3), "reasons": reasons, "relevant": bool(touched or risk)})

    scored.sort(key=lambda s: -s["score"])
    total = sum(t.duration_s for t in tests) or 1.0
    budget, used = total * budget_ratio, 0.0
    for s in scored:
        t = s["test"]
        must = t.kind == "smoke" or (t.is_regression and s["relevant"]) or (t.module in mods)
        if must or (s["score"] > 0.3 and used + t.duration_s <= budget):
            s["selected"] = True
            used += t.duration_s
        else:
            s["selected"] = False
            if not s["reasons"]:
                s["reasons"] = ["no impact from this change"]
    return scored


def pipeline_risk(db: Session, files: list[str]) -> tuple[float, list[str]]:
    mods = changed_modules(files)
    risks = {r.module: r for r in db.scalars(select(ModuleRisk))}
    factors, score = [], 0.1 * min(len(files), 5) / 5
    for m in mods:
        r = risks.get(m)
        if r and r.risk_score:
            score += 0.6 * r.risk_score
            factors.append(f"{m}: {r.incident_count} verified incident(s), risk {r.risk_score:.2f}")
    if any(f.startswith(("config/", "migrations/")) for f in files):
        score += 0.15
        factors.append("touches configuration or migrations")
    return round(min(1.0, score), 3), factors
