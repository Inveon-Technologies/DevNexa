import os
import tempfile

os.environ["STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, state  # noqa: E402

client = TestClient(app)


def setup_function():
    state.reset()


def test_healthy_by_default():
    assert client.get("/health").status_code == 200
    assert client.get("/orders/7").status_code == 200
    assert "http_requests_total" in client.get("/metrics").text


def test_app_exception_fault_and_rollback():
    client.post("/admin/deploy", json={"version": "v9", "defect": "app_exception"})
    assert client.get("/orders/7").status_code == 500  # order 7 has no currency
    r = client.post("/admin/rollback", json={"version": "v1.4.0"}).json()
    assert r["resolved_faults"] == ["app_exception"]
    assert client.get("/orders/7").status_code == 200


def test_invalid_config_breaks_health_until_reverted():
    client.post("/admin/faults/invalid_config")
    assert client.get("/health").status_code == 503
    assert client.post("/admin/actions/restart_container").json()["resolved_faults"] == []
    assert client.post("/admin/actions/revert_config").json()["resolved_faults"] == ["invalid_config"]
    assert client.get("/health").status_code == 200


def test_dependency_failure():
    client.post("/admin/faults/dependency_failure")
    assert client.post("/checkout", json={"order_id": 1}).status_code == 502
    client.post("/admin/actions/restart_dependency")
    assert client.post("/checkout", json={"order_id": 1}).status_code == 200


def test_unknown_fault_and_action():
    assert client.post("/admin/faults/nope").status_code == 404
    assert client.post("/admin/actions/nope").status_code == 404
