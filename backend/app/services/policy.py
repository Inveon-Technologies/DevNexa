"""Graded autonomy: decide whether a remediation action may run automatically."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import KeyValue, PolicyRule
from ..simulation.catalog import ACTIONS

DEFAULT_AUTONOMY = {
    "auto_execute_low": True,      # allowlisted low-risk actions run without a human
    "auto_execute_medium": False,  # a configured policy may allow medium-risk automation
    "min_confidence_for_auto": 0.55,
}


@dataclass
class PolicyDecision:
    risk_level: str
    decision: str  # auto_execute | recommend | approval_required | blocked
    reason: str


def seed_policies(db: Session) -> None:
    for a in ACTIONS.values():
        if not db.get(PolicyRule, a.type):
            db.add(PolicyRule(action_type=a.type, title=a.title, description=a.description,
                              risk_level=a.risk, allowlisted=a.allowlisted))
    if not db.get(KeyValue, "autonomy"):
        db.add(KeyValue(key="autonomy", value=dict(DEFAULT_AUTONOMY)))
    db.flush()


def get_autonomy(db: Session) -> dict:
    row = db.get(KeyValue, "autonomy")
    return {**DEFAULT_AUTONOMY, **(row.value if row else {})}


def set_autonomy(db: Session, values: dict) -> dict:
    row = db.get(KeyValue, "autonomy")
    merged = {**get_autonomy(db), **{k: v for k, v in values.items() if k in DEFAULT_AUTONOMY}}
    if row:
        row.value = merged
    else:
        db.add(KeyValue(key="autonomy", value=merged))
    db.flush()
    return merged


def evaluate(db: Session, action_type: str, confidence: float) -> PolicyDecision:
    rule = db.get(PolicyRule, action_type)
    spec = ACTIONS[action_type]
    risk = rule.risk_level if rule else spec.risk
    allowlisted = rule.allowlisted if rule else spec.allowlisted
    if rule is not None and not rule.enabled:
        return PolicyDecision(risk, "blocked", "Action is disabled by policy.")
    auto = get_autonomy(db)
    if risk == "high":
        return PolicyDecision(risk, "approval_required",
                              "High-risk action: explicit human approval is required before execution.")
    if risk == "medium":
        if auto["auto_execute_medium"] and allowlisted and confidence >= auto["min_confidence_for_auto"]:
            return PolicyDecision(risk, "auto_execute", "Medium-risk action allowlisted by configured policy.")
        return PolicyDecision(risk, "recommend",
                              "Medium-risk action: recommended with evidence; awaiting approval per policy.")
    if not allowlisted:
        return PolicyDecision(risk, "recommend", "Low-risk action is not on the allowlist; needs approval.")
    if not auto["auto_execute_low"]:
        return PolicyDecision(risk, "recommend", "Automatic execution of low-risk actions is switched off.")
    if confidence < auto["min_confidence_for_auto"] and action_type != "escalate":
        return PolicyDecision(risk, "recommend",
                              f"Diagnosis confidence {confidence:.2f} is below the automation threshold "
                              f"{auto['min_confidence_for_auto']:.2f}; needs approval.")
    return PolicyDecision(risk, "auto_execute", "Low-risk allowlisted action: executing automatically.")
