def test_health_and_system(client):
    assert client.get("/api/health").json()["status"] == "ok"
    s = client.get("/api/system").json()
    assert s["environment_mode"] == "simulated"
    assert s["database"] == "sqlite"


def test_pipeline_success_and_prioritisation(client):
    r = client.post("/api/pipelines", json={"message": "feat: x", "files": ["app/orders/routes.py"]})
    assert r.status_code == 201
    run = client.get(f"/api/pipelines/{r.json()['id']}").json()
    assert run["status"] == "success"
    assert [s["status"] for s in run["stages"]] == ["success"] * 4
    selected = [t for t in run["tests"] if t["selected"]]
    assert 0 < len(selected) < len(run["tests"])
    assert run["deployment"]["status"] == "active"


def test_build_failure_returns_ai_feedback(client):
    r = client.post("/api/pipelines", json={"message": "deps", "files": ["requirements.txt"],
                                            "defect": "build_dependency_conflict"}).json()
    run = client.get(f"/api/pipelines/{r['id']}").json()
    assert run["status"] == "failed"
    assert run["stages"][0]["status"] == "failed"
    assert "pydantic" in run["ai_feedback"]


def test_test_failure_feedback(client):
    r = client.post("/api/pipelines", json={"message": "totals", "files": ["app/orders/routes.py"],
                                            "defect": "unit_test_regression"}).json()
    run = client.get(f"/api/pipelines/{r['id']}").json()
    assert run["status"] == "failed"
    assert any(t["status"] == "failed" for t in run["tests"])


def test_low_risk_fault_is_auto_remediated_and_learned(client):
    before = len(client.get("/api/knowledge").json())
    assert client.post("/api/faults/dependency_failure/inject", json={}).json()["mode"] == "runtime"
    inc = client.get("/api/incidents").json()[0]
    assert inc["status"] == "resolved"
    detail = client.get(f"/api/incidents/{inc['id']}").json()
    assert detail["diagnoses"][0]["root_cause_id"] == "dependency_failure"
    assert detail["actions"][0]["decision"] == "auto_execute"
    assert detail["health_checks"][-1]["passed"]
    assert len(client.get("/api/knowledge").json()) == before + 1
    assert client.get("/api/environment").json()["healthy"]


def test_high_risk_fault_requires_approval(client):
    client.post("/api/faults/cert_expired/inject", json={})
    inc = client.get("/api/incidents").json()[0]
    assert inc["status"] == "awaiting_approval"
    action = inc["pending_action"]
    assert action["risk_level"] == "high" and action["decision"] == "approval_required"
    assert client.get("/api/actions?status=pending_approval").json()[0]["id"] == action["id"]
    client.post(f"/api/actions/{action['id']}/approve", json={"actor": "tester"})
    detail = client.get(f"/api/incidents/{inc['id']}").json()
    assert detail["status"] == "resolved"
    assert detail["human_interventions"] >= 1
    assert any(w["step"] == "learn" for w in detail["workflow"])


def test_rejected_action_and_reanalysis(client):
    client.post("/api/faults/db_pool_exhaustion/inject", json={})
    inc = client.get("/api/incidents").json()[0]
    action = inc["pending_action"]
    client.post(f"/api/actions/{action['id']}/reject", json={"actor": "tester", "note": "check index"})
    r = client.post(f"/api/incidents/{inc['id']}/reanalyze", json={"actor": "tester"}).json()
    assert "db_pool_exhaustion" in r["excluded"]
    detail = client.get(f"/api/incidents/{inc['id']}").json()
    assert len(detail["diagnoses"]) == 2
    assert detail["diagnoses"][1]["root_cause_id"] != "db_pool_exhaustion"
    client.post(f"/api/incidents/{inc['id']}/close", params={"actor": "tester"})
    client.post("/api/environment/reset")


def test_blocked_action_escalates(client):
    # With rollback disabled by policy the proposed action is blocked and the incident is escalated,
    # never reported as fixed.
    client.patch("/api/policies/rollback_deployment", json={"enabled": False})
    r = client.post("/api/faults/app_exception/inject", json={}).json()
    assert r["mode"] == "pipeline"
    inc = client.get("/api/incidents").json()[0]
    assert inc["status"] == "escalated"
    client.patch("/api/policies/rollback_deployment", json={"enabled": True})
    client.post(f"/api/incidents/{inc['id']}/close")
    client.post("/api/environment/reset")


def test_failed_verification_triggers_reanalysis(client):
    from app.services.environment import get_environment

    env = get_environment()
    env.inject_fault("dependency_failure")
    env.inject_fault("memory_leak")
    from app.agents.runner import start_monitor_scan

    start_monitor_scan(0)
    inc = client.get("/api/incidents").json()[0]
    detail = client.get(f"/api/incidents/{inc['id']}").json()
    assert detail["status"] == "resolved"
    assert detail["attempts"] == 2
    assert [d["outcome"] for d in detail["diagnoses"]] == ["rejected", "confirmed"]
    assert [h["passed"] for h in detail["health_checks"]] == [False, True]


def test_feedback_loop_prevents_recurrence(client):
    # First occurrence reaches production and is learned ...
    client.post("/api/faults/memory_leak/inject", json={})
    inc = client.get("/api/incidents").json()[0]
    assert inc["status"] == "resolved"
    tests = {t["id"]: t for t in client.get("/api/tests").json()}
    assert tests["tests/test_cache.py::test_cache_eviction_bound"]["is_regression"]
    # ... the same defect shipped again is caught by the regression test in CI.
    r = client.post("/api/faults/memory_leak/inject", json={}).json()
    run = client.get(f"/api/pipelines/{r['run_id']}").json()
    assert run["status"] == "failed" and run["stages"][1]["status"] == "failed"
    assert "Regression test" in run["ai_feedback"]
    risk = {m["module"]: m for m in client.get("/api/modules/risk").json()}
    assert risk["cache"]["risk_score"] > 0


def test_policy_guards(client):
    assert client.patch("/api/policies/apply_db_index", json={"allowlisted": True}).status_code == 400
    assert client.patch("/api/policies/clear_cache", json={"allowlisted": False}).json()["allowlisted"] is False
    client.patch("/api/policies/clear_cache", json={"allowlisted": True})


def test_knowledge_search(client):
    res = client.get("/api/knowledge/search", params={"q": "connection refused payments-gateway"}).json()
    assert res["results"][0]["root_cause_id"] == "dependency_failure"


def test_alertmanager_webhook_creates_incident(client):
    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "OrdersHighLatency", "service": "other-svc",
                                                           "severity": "warning"},
                           "annotations": {"summary": "p95 high"}, "fingerprint": "abc"}]}
    r = client.post("/api/webhooks/alertmanager", json=payload).json()
    assert r["incident_id"]
    client.post(f"/api/incidents/{r['incident_id']}/close")


def test_github_actions_report(client):
    base = {"run_key": "123-1", "commit_sha": "f" * 40, "message": "ci", "files": ["app/health.py"]}
    client.post("/api/webhooks/github/actions", json={**base, "stage": "build", "status": "success"})
    r = client.post("/api/webhooks/github/actions", json={**base, "stage": "test", "status": "failed",
                                                          "log": "FAILED tests/test_health.py::test_liveness - assert 503 == 200"}).json()
    assert "test_liveness" in r["ai_feedback"]
    run = client.get(f"/api/pipelines/{r['run_id']}").json()
    assert run["trigger"] == "github-actions" and run["status"] == "failed"


def test_github_push_webhook(client):
    payload = {"ref": "refs/heads/main", "after": "e" * 40, "repository": {"full_name": "org/repo"},
               "head_commit": {"id": "e" * 40, "message": "fix: thing", "author": {"username": "dev"}},
               "commits": [{"modified": ["app/checkout/routes.py"]}]}
    r = client.post("/api/webhooks/github", json=payload, headers={"X-GitHub-Event": "push"}).json()
    run = client.get(f"/api/pipelines/{r['run_id']}").json()
    assert run["changed_files"] == ["app/checkout/routes.py"] and run["trigger"] == "github-push"


def test_experiment_runs(client):
    r = client.post("/api/experiments", json={"config": {"repetitions": 2, "scenarios": ["memory_leak", "cert_expired"]},
                                              "sync": True}).json()
    e = client.get(f"/api/experiments/{r['id']}").json()
    assert e["status"] == "completed"
    v = e["results"]["variants"]
    assert set(v) == {"devnexa", "no_rag", "baseline"}
    assert v["devnexa"]["metrics"]["prevented_in_ci"] >= 1
    assert v["baseline"]["metrics"]["repeated_incidents"] >= v["devnexa"]["metrics"]["repeated_incidents"]


def test_overview(client):
    o = client.get("/api/overview").json()
    assert o["kpis"]["pipeline_runs"] > 0
    assert len(o["timeline"]) == 8
