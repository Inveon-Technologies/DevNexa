"""Target environment the agents act on.

``SimulatedEnvironment`` models the sample orders-service in memory so the full loop can be demonstrated
(and evaluated) without Docker. ``LiveEnvironment`` drives the real sample app started by docker-compose
through its admin API, Prometheus, and (optionally) the Docker CLI.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from typing import Callable

import httpx

from ..config import get_settings
from ..simulation.catalog import ACTIONS, BASE_METRICS, FAULTS, SERVICE

THRESHOLDS = {
    "http_error_rate": ("Error rate", 0.05, "lt"),
    "http_p95_latency_ms": ("p95 latency (ms)", 1000.0, "lt"),
    "memory_mb": ("Memory (MB)", 450.0, "lt"),
    "upstream_error_rate": ("Upstream error rate", 0.10, "lt"),
}

DEFAULT_STATE = {
    "service": SERVICE,
    "version": "v1.4.0",
    "image": "devnexa/sample-app:v1.4.0",
    "commit_sha": "a1c9e04",
    "active_faults": {},
    "config": {"payment_timeout_ms": 3000, "db_pool_size": 5, "cache_maxsize": None},
    "events": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimulatedEnvironment:
    mode = "simulated"

    def __init__(self, state: dict | None = None, on_change: Callable[[dict], None] | None = None,
                 now_fn: Callable[[], datetime] | None = None):
        self._state = copy.deepcopy(state or DEFAULT_STATE)
        self._lock = threading.RLock()
        self._on_change = on_change
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _ts(self) -> str:
        return self._now_fn().isoformat()

    # ----------------------------------------------------------------- state
    @property
    def state(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def _changed(self, event: str, **details):
        self._state["events"] = (self._state.get("events", []) + [{"ts": self._ts(), "event": event, **details}])[-50:]
        if self._on_change:
            self._on_change(self.state)

    def reset(self):
        with self._lock:
            self._state = copy.deepcopy(DEFAULT_STATE)
            self._changed("reset")

    # ---------------------------------------------------------------- faults
    def inject_fault(self, fault_id: str, via_deploy: bool = False) -> dict:
        if fault_id not in FAULTS:
            raise ValueError(f"unknown fault scenario {fault_id!r}")
        with self._lock:
            info = {"started_at": self._ts(), "via_deploy": via_deploy, "version": self._state["version"]}
            self._state["active_faults"][fault_id] = info
            self._changed("fault_injected", fault=fault_id)
            return info

    def clear_faults(self):
        with self._lock:
            self._state["active_faults"] = {}
            self._changed("faults_cleared")

    def active_faults(self) -> dict:
        return self.state["active_faults"]

    # ----------------------------------------------------------- deployments
    def deploy(self, version: str, image: str, commit_sha: str, defect: str | None = None) -> dict:
        with self._lock:
            self._state.update(version=version, image=image, commit_sha=commit_sha)
            self._changed("deployed", version=version)
        if defect and defect in FAULTS:
            self.inject_fault(defect, via_deploy=True)
        return {"ok": True, "version": version}

    def rollback(self, version: str, image: str, commit_sha: str) -> dict:
        with self._lock:
            removed = [f for f, info in self._state["active_faults"].items() if info.get("via_deploy")]
            for f in removed:
                self._state["active_faults"].pop(f, None)
            self._state.update(version=version, image=image, commit_sha=commit_sha)
            self._changed("rolled_back", version=version, removed=removed)
        return {"ok": True, "version": version, "resolved_faults": removed}

    # --------------------------------------------------------------- actions
    def execute_action(self, action_type: str, params: dict | None = None) -> dict:
        if action_type not in ACTIONS:
            raise ValueError(f"unknown action {action_type!r}")
        params = params or {}
        if action_type == "rollback_deployment":
            return self.rollback(params.get("version", "previous"), params.get("image", ""),
                                 params.get("commit_sha", ""))
        with self._lock:
            resolved = [f for f in list(self._state["active_faults"])
                        if action_type in FAULTS[f].fix_actions]
            for f in resolved:
                self._state["active_faults"].pop(f, None)
            cfg = self._state["config"]
            if action_type == "increase_pool_size":
                cfg["db_pool_size"] = max(cfg.get("db_pool_size", 5), 20)
            elif action_type == "revert_config":
                cfg["payment_timeout_ms"] = 3000
            self._changed("action_executed", action=action_type, resolved=resolved)
        return {"ok": True, "action": action_type, "resolved_faults": resolved,
                "output": _ACTION_OUTPUT.get(action_type, "done")}

    # ------------------------------------------------------------- telemetry
    def metrics(self) -> dict[str, float]:
        values = dict(BASE_METRICS)
        for f in self.active_faults():
            for name, (_, faulty) in FAULTS[f].metrics.items():
                if name == "cpu_usage":
                    values[name] = faulty
                else:
                    values[name] = max(values[name], faulty)
        return values

    def health(self) -> list[dict]:
        m = self.metrics()
        faults = self.active_faults()
        checks = [{
            "name": "Liveness probe",
            "passed": "invalid_config" not in faults,
            "value": "down" if "invalid_config" in faults else "up",
            "threshold": "up",
        }]
        for key, (label, limit, _) in THRESHOLDS.items():
            checks.append({"name": label, "metric": key, "value": round(m[key], 4), "threshold": limit,
                           "passed": m[key] < limit})
        return checks


_ACTION_OUTPUT = {
    "restart_container": "container devnexa-sample-app restarted; readiness OK after 6s",
    "restart_dependency": "container payments-gateway restarted; /health 200",
    "clear_cache": "cache namespace 'responses' flushed (allowlisted)",
    "revert_config": "config/settings.yaml restored to revision r41; workers reloaded",
    "increase_pool_size": "DB_POOL_SIZE 5 -> 20, DB_MAX_OVERFLOW 0 -> 10; rolling restart complete",
    "apply_db_index": "migration 0042_orders_customer_email_idx applied (CREATE INDEX CONCURRENTLY)",
    "rotate_certificate": "client certificate renewed (notAfter +90d) and reloaded",
    "rerun_job": "job re-run completed",
    "escalate": "on-call engineer paged with incident evidence bundle",
}


class LiveEnvironment:
    """Talks to the docker-compose deployment of the sample app."""

    mode = "live"

    def __init__(self):
        self.s = get_settings()
        self.client = httpx.Client(timeout=5.0)

    def _admin(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, f"{self.s.sample_app_url}{path}", **kw)
        r.raise_for_status()
        return r.json()

    @property
    def state(self) -> dict:
        try:
            return self._admin("GET", "/admin/state")
        except httpx.HTTPError as e:
            return {**DEFAULT_STATE, "error": str(e)}

    def reset(self):
        self._admin("POST", "/admin/reset")

    def inject_fault(self, fault_id: str, via_deploy: bool = False) -> dict:
        return self._admin("POST", f"/admin/faults/{fault_id}", params={"via_deploy": via_deploy})

    def clear_faults(self):
        self._admin("DELETE", "/admin/faults")

    def active_faults(self) -> dict:
        return self.state.get("active_faults", {})

    def deploy(self, version: str, image: str, commit_sha: str, defect: str | None = None) -> dict:
        return self._admin("POST", "/admin/deploy",
                           json={"version": version, "image": image, "commit_sha": commit_sha, "defect": defect})

    def rollback(self, version: str, image: str, commit_sha: str) -> dict:
        return self._admin("POST", "/admin/rollback",
                           json={"version": version, "image": image, "commit_sha": commit_sha})

    def execute_action(self, action_type: str, params: dict | None = None) -> dict:
        params = params or {}
        if action_type == "rollback_deployment":
            return self.rollback(params.get("version", "previous"), params.get("image", ""),
                                 params.get("commit_sha", ""))
        result = self._admin("POST", f"/admin/actions/{action_type}", json=params)
        if action_type == "restart_container" and shutil.which("docker"):
            proc = subprocess.run(["docker", "restart", self.s.sample_app_container],
                                  capture_output=True, text=True, timeout=60)
            result["docker"] = (proc.stdout or proc.stderr).strip()
        return result

    def _prom(self, query: str) -> float | None:
        try:
            r = self.client.get(f"{self.s.prometheus_url}/api/v1/query", params={"query": query})
            data = r.json()["data"]["result"]
            return float(data[0]["value"][1]) if data else 0.0
        except Exception:
            return None

    def metrics(self) -> dict[str, float]:
        queries = {
            "http_error_rate": 'sum(rate(http_requests_total{status=~"5.."}[1m])) / '
                               "clamp_min(sum(rate(http_requests_total[1m])), 1e-9)",
            "http_p95_latency_ms": "histogram_quantile(0.95, sum by (le) "
                                   "(rate(http_request_duration_seconds_bucket[1m]))) * 1000",
            "memory_mb": "max(orders_memory_mb)",
            "upstream_error_rate": "max(orders_upstream_error_ratio)",
            "db_connections_in_use": "max(orders_db_connections_in_use)",
        }
        values = dict(BASE_METRICS)
        for k, q in queries.items():
            v = self._prom(q)
            if v is not None and v == v:  # skip NaN
                values[k] = v
        try:
            values.update({k: v for k, v in self._admin("GET", "/admin/metrics").items() if k in values})
        except Exception:
            pass
        return values

    def health(self) -> list[dict]:
        try:
            r = self.client.get(f"{self.s.sample_app_url}/health")
            live = r.status_code == 200
        except httpx.HTTPError:
            live = False
        m = self.metrics()
        checks = [{"name": "Liveness probe", "passed": live, "value": "up" if live else "down", "threshold": "up"}]
        for key, (label, limit, _) in THRESHOLDS.items():
            checks.append({"name": label, "metric": key, "value": round(m[key], 4), "threshold": limit,
                           "passed": m[key] < limit})
        return checks


_env = None
_env_lock = threading.Lock()


def get_environment():
    """Process-wide environment. The simulated one persists its state in the kv_store table."""
    global _env
    with _env_lock:
        if _env is None:
            if get_settings().is_live:
                _env = LiveEnvironment()
            else:
                from ..db import session_scope
                from ..models import KeyValue

                with session_scope() as db:
                    row = db.get(KeyValue, "sim_environment")
                    state = row.value if row else None

                def persist(st: dict):
                    with session_scope() as db:
                        row = db.get(KeyValue, "sim_environment")
                        if row:
                            row.value = st
                        else:
                            db.add(KeyValue(key="sim_environment", value=st))

                _env = SimulatedEnvironment(state, persist)
        return _env


def set_environment(env) -> None:
    global _env
    _env = env
