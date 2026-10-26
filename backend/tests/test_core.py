from app.agents.context import RunContext, SimClock
from app.services.analyzer import DiagnosisContext, HeuristicAnalyzer
from app.services.embeddings import HashingEmbedder, cosine
from app.services.environment import SimulatedEnvironment
from app.simulation.catalog import FAULTS, module_for_file


def test_embedding_similarity_orders_related_text():
    e = HashingEmbedder(384)
    a = e.embed("QueuePool limit of size 5 overflow 0 reached connection timed out")
    b = e.embed("sqlalchemy QueuePool limit reached, connection timed out waiting for pool")
    c = e.embed("certificate verify failed: certificate has expired")
    assert cosine(a, b) > cosine(a, c)
    assert abs(sum(x * x for x in a) - 1) < 1e-6


def test_module_mapping():
    assert module_for_file("app/orders/serializers.py") == "api/orders"
    assert module_for_file("migrations/0042_idx.py") == "db/repository"
    assert module_for_file("README.md") is None


def test_simulated_environment_fault_and_fix():
    env = SimulatedEnvironment()
    assert all(c["passed"] for c in env.health())
    env.inject_fault("memory_leak")
    assert not all(c["passed"] for c in env.health())
    assert env.execute_action("rollback_deployment")["resolved_faults"] == []  # not shipped via deploy
    assert not all(c["passed"] for c in env.health())
    assert env.execute_action("restart_container")["resolved_faults"] == ["memory_leak"]
    assert all(c["passed"] for c in env.health())


def test_rollback_removes_deploy_faults():
    env = SimulatedEnvironment()
    env.deploy("v2", "img:v2", "abc", defect="app_exception")
    assert "app_exception" in env.active_faults()
    env.execute_action("rollback_deployment", {"version": "v1"})
    assert env.active_faults() == {}


def _evidence_for(fault_id, gap=0.0):
    import random

    from app.services.telemetry import simulated_evidence

    class Inc:
        id, alert_name, service, summary = 1, "Alert", "orders-service", ""

    class FakeDB:
        def scalars(self, *_):
            class R:
                def all(self):
                    return []

            return R()

    env = SimulatedEnvironment()
    env.inject_fault(fault_id)
    return simulated_evidence(FakeDB(), Inc(), env.active_faults(), random.Random(1), gap)


def test_heuristic_analyzer_identifies_each_fault_with_full_evidence():
    a = HeuristicAnalyzer()
    for fid in FAULTS:
        res = a.diagnose(DiagnosisContext("Alert", "orders-service", "", _evidence_for(fid), []))
        assert res.root_cause_id == fid, (fid, res.root_cause_id)
        assert 0 < res.confidence <= 1
        assert res.supporting_evidence


def test_analyzer_respects_excluded_causes():
    a = HeuristicAnalyzer()
    res = a.diagnose(DiagnosisContext("Alert", "svc", "", _evidence_for("slow_query"), [],
                                      excluded_causes=["slow_query"]))
    assert res.root_cause_id != "slow_query"


def test_rag_uses_similar_verified_incident():
    a = HeuristicAnalyzer()
    similar = [{"knowledge_id": 9, "incident_id": 9, "title": "past", "root_cause_id": "cert_expired",
                "root_cause": "x", "action_type": "rotate_certificate", "outcome": "resolved", "similarity": 0.9}]
    res = a.diagnose(DiagnosisContext("Alert", "svc", "", _evidence_for("cert_expired", gap=1.0), similar))
    assert res.root_cause_id == "cert_expired"
    assert any(s["kind"] == "similar_incident" for s in res.supporting_evidence)


def test_sim_clock():
    c = SimClock()
    t0 = c.now()
    c.advance(90)
    assert (c.now() - t0).total_seconds() == 90
    assert RunContext  # imported for API stability
