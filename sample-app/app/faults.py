"""Fault state of the sample service, controllable through the admin API for controlled experiments.

Fault ids and the actions that fix them mirror backend/app/simulation/catalog.py.
"""

import json
import os
import threading
from datetime import datetime, timezone

FIXES = {
    "invalid_config": {"revert_config", "rollback_deployment"},
    "dependency_failure": {"restart_dependency"},
    "app_exception": {"rollback_deployment"},
    "slow_query": {"apply_db_index"},
    "memory_leak": {"restart_container", "clear_cache"},
    "db_pool_exhaustion": {"increase_pool_size"},
    "cert_expired": {"rotate_certificate"},
}
KNOWN_ACTIONS = {"rerun_job", "restart_container", "restart_dependency", "clear_cache", "rollback_deployment",
                 "revert_config", "increase_pool_size", "apply_db_index", "rotate_certificate", "escalate"}

STATE_FILE = os.getenv("STATE_FILE", "/tmp/sample-app-state.json")
DEFAULT = {
    "service": "orders-service", "version": "v1.4.0", "image": "devnexa/sample-app:v1.4.0", "commit_sha": "a1c9e04",
    "active_faults": {}, "config": {"payment_timeout_ms": 3000, "db_pool_size": 5, "cache_maxsize": None},
    "events": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FaultState:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.state = json.loads(json.dumps(DEFAULT))
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.state.update(json.load(f))
            except (OSError, ValueError):
                pass

    def _save(self, event: str, **details):
        self.state["events"] = (self.state["events"] + [{"ts": _now(), "event": event, **details}])[-50:]
        try:
            with open(self.path, "w") as f:
                json.dump(self.state, f)
        except OSError:
            pass

    def active(self, fault: str) -> bool:
        return fault in self.state["active_faults"]

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def inject(self, fault: str, via_deploy: bool = False):
        if fault not in FIXES:
            raise KeyError(fault)
        with self.lock:
            self.state["active_faults"][fault] = {"started_at": _now(), "via_deploy": via_deploy,
                                                  "version": self.state["version"]}
            self._save("fault_injected", fault=fault)

    def clear(self):
        with self.lock:
            self.state["active_faults"] = {}
            self._save("faults_cleared")

    def reset(self):
        with self.lock:
            self.state = json.loads(json.dumps(DEFAULT))
            self._save("reset")

    def deploy(self, version: str, image: str, commit_sha: str, defect: str | None):
        with self.lock:
            self.state.update(version=version, image=image, commit_sha=commit_sha)
            self._save("deployed", version=version)
        if defect in FIXES:
            self.inject(defect, via_deploy=True)

    def rollback(self, version: str, image: str, commit_sha: str) -> list[str]:
        with self.lock:
            removed = [f for f, i in self.state["active_faults"].items() if i.get("via_deploy")]
            for f in removed:
                del self.state["active_faults"][f]
            self.state.update(version=version, image=image or self.state["image"], commit_sha=commit_sha)
            self._save("rolled_back", version=version, removed=removed)
            return removed

    def apply(self, action: str) -> list[str]:
        if action not in KNOWN_ACTIONS:
            raise KeyError(action)
        with self.lock:
            resolved = [f for f in list(self.state["active_faults"]) if action in FIXES[f]]
            for f in resolved:
                del self.state["active_faults"][f]
            if action == "increase_pool_size":
                self.state["config"]["db_pool_size"] = 20
            if action == "revert_config":
                self.state["config"]["payment_timeout_ms"] = 3000
            self._save("action_executed", action=action, resolved=resolved)
            return resolved
