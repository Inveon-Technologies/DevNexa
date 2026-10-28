from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .config import get_settings
from .db import Base, EmbeddingType

EMBED_DIM = get_settings().embedding_dim


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """Timezone-aware UTC DateTime that also round-trips through SQLite (which drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


# ---------------------------------------------------------------- CI/CD layer
class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(200), default="devnexa/sample-app")
    branch: Mapped[str] = mapped_column(String(120), default="main")
    commit_sha: Mapped[str] = mapped_column(String(64))
    commit_message: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(120), default="developer")
    changed_files: Mapped[list] = mapped_column(JSON, default=list)
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    current_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    test_strategy: Mapped[str] = mapped_column(String(20), default="prioritized")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_factors: Mapped[list] = mapped_column(JSON, default=list)
    ai_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    injected_defect: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    stages: Mapped[list["StageRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="StageRun.id"
    )
    tests: Mapped[list["TestExecution"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="TestExecution.order"
    )


class StageRun(Base):
    __tablename__ = "stage_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(20))
    agent: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    logs: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="stages")


class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    module: Mapped[str] = mapped_column(String(80))
    covers: Mapped[list] = mapped_column(JSON, default=list)
    detects: Mapped[list] = mapped_column(JSON, default=list)
    duration_s: Mapped[float] = mapped_column(Float, default=1.0)
    kind: Mapped[str] = mapped_column(String(20), default="unit")
    is_regression: Mapped[bool] = mapped_column(Boolean, default=False)
    origin_incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class TestExecution(Base):
    __tablename__ = "test_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="CASCADE"))
    test_id: Mapped[str] = mapped_column(String(200))
    module: Mapped[str] = mapped_column(String(80))
    order: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="tests")


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service: Mapped[str] = mapped_column(String(80), default="orders-service")
    version: Mapped[str] = mapped_column(String(40))
    image: Mapped[str] = mapped_column(String(200))
    commit_sha: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(40), default="staging")
    status: Mapped[str] = mapped_column(String(20), default="active")
    health: Mapped[str] = mapped_column(String(20), default="healthy")
    is_stable: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


# ---------------------------------------------------------- incident handling
class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    service: Mapped[str] = mapped_column(String(80), default="orders-service")
    status: Mapped[str] = mapped_column(String(20), default="firing")
    summary: Mapped[str] = mapped_column(Text, default="")
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(40), default="alertmanager")
    starts_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    received_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(30), default="open")
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    service: Mapped[str] = mapped_column(String(80), default="orders-service")
    summary: Mapped[str] = mapped_column(Text, default="")
    alert_name: Mapped[str] = mapped_column(String(120), default="")
    # Ground truth for controlled fault-injection experiments (never shown to the analyzer).
    fault_scenario: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fault_started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    human_interventions: Mapped[int] = mapped_column(Integer, default=0)
    workflow: Mapped[list] = mapped_column(JSON, default=list)
    confirmed_cause_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_recurrence: Mapped[bool] = mapped_column(Boolean, default=False)

    evidence: Mapped[list["EvidenceItem"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="EvidenceItem.id"
    )
    diagnoses: Mapped[list["Diagnosis"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="Diagnosis.id"
    )
    actions: Mapped[list["RemediationAction"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="RemediationAction.id"
    )
    health_checks: Mapped[list["HealthCheck"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="HealthCheck.id"
    )


class EvidenceItem(Base):
    __tablename__ = "evidence_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(240))
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    root_cause_id: Mapped[str] = mapped_column(String(80))
    root_cause: Mapped[str] = mapped_column(Text)
    affected_component: Mapped[str] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    uncertainty: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    supporting_evidence: Mapped[list] = mapped_column(JSON, default=list)
    alternatives: Mapped[list] = mapped_column(JSON, default=list)
    similar_incidents: Mapped[list] = mapped_column(JSON, default=list)
    proposed_action: Mapped[str] = mapped_column(String(60))
    provider: Mapped[str] = mapped_column(String(40), default="heuristic")
    used_rag: Mapped[bool] = mapped_column(Boolean, default=True)
    outcome: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="diagnoses")


class RemediationAction(Base):
    __tablename__ = "remediation_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    diagnosis_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_type: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_level: Mapped[str] = mapped_column(String(10))
    decision: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="proposed")
    requested_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="actions")


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True
    )
    action_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target: Mapped[str] = mapped_column(String(120))
    passed: Mapped[bool] = mapped_column(Boolean)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    incident: Mapped[Incident | None] = relationship(back_populates="health_checks")


# ------------------------------------------------------- knowledge & learning
class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    symptoms: Mapped[str] = mapped_column(Text)
    root_cause_id: Mapped[str] = mapped_column(String(80))
    root_cause: Mapped[str] = mapped_column(Text)
    action_type: Mapped[str] = mapped_column(String(60))
    outcome: Mapped[str] = mapped_column(String(40), default="resolved")
    service: Mapped[str] = mapped_column(String(80), default="orders-service")
    affected_module: Mapped[str] = mapped_column(String(80))
    missed_test: Mapped[str | None] = mapped_column(String(200), nullable=True)
    regression_test: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    mttr_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    times_retrieved: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list | None] = mapped_column(EmbeddingType(EMBED_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class ModuleRisk(Base):
    __tablename__ = "module_risk"

    module: Mapped[str] = mapped_column(String(80), primary_key=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    incident_count: Mapped[int] = mapped_column(Integer, default=0)
    last_incident_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    action_type: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(10))
    allowlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="running")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class KeyValue(Base):
    """Small persistent store, e.g. for the simulated environment's state."""

    __tablename__ = "kv_store"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


# ------------------------------------------------------ user repo deployments
class AppDeployment(Base):
    """One deploy of a user's GitHub repo as a container, with its logs, health and agent findings."""

    __tablename__ = "app_deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    app: Mapped[str] = mapped_column(String(60), index=True)
    repo: Mapped[str] = mapped_column(String(200))
    branch: Mapped[str] = mapped_column(String(120), default="main")
    root_dir: Mapped[str] = mapped_column(String(200), default="")
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    health: Mapped[str] = mapped_column(String(20), default="unknown")
    build_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    image: Mapped[str | None] = mapped_column(String(200), nullable=True)
    container: Mapped[str | None] = mapped_column(String(120), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    build_log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    remediations: Mapped[list] = mapped_column(JSON, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(120), default="operator")
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
