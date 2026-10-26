"""Static knowledge about the controlled environment used for the prototype.

* ``MODULES``      - modules of the sample "orders-service" and the files that belong to them
* ``TEST_SUITE``   - its baseline automated test suite
* ``ACTIONS``      - remediation actions DevNexa knows how to execute, with their default risk level
* ``FAULTS``       - controlled fault-injection scenarios (ground truth for experiments)
* ``CI_DEFECTS``   - defects that can be injected into a commit to exercise build/test failure feedback
"""

from __future__ import annotations

from dataclasses import dataclass, field

SERVICE = "orders-service"

MODULES: dict[str, list[str]] = {
    "api/orders": ["app/orders/routes.py", "app/orders/serializers.py"],
    "api/checkout": ["app/checkout/routes.py"],
    "integrations/payments": ["app/integrations/payments_client.py"],
    "db/repository": ["app/db/repository.py", "migrations/"],
    "db/session": ["app/db/session.py"],
    "cache": ["app/cache/lru.py"],
    "auth": ["app/auth/tls.py", "app/auth/tokens.py"],
    "config": ["config/settings.yaml", "app/config.py"],
    "api/health": ["app/health.py"],
}


def module_for_file(path: str) -> str | None:
    for module, files in MODULES.items():
        for f in files:
            if path == f or (f.endswith("/") and path.startswith(f)):
                return module
    for module, files in MODULES.items():
        prefix = files[0].rsplit("/", 1)[0]
        if path.startswith(prefix + "/"):
            return module
    return None


@dataclass(frozen=True)
class TestSpec:
    id: str
    module: str
    covers: tuple[str, ...]
    duration_s: float
    kind: str = "unit"
    detects: tuple[str, ...] = ()


def _t(id_, module, covers=None, duration=1.0, kind="unit", detects=()):
    return TestSpec(id_, module, tuple(covers or [module]), duration, kind, tuple(detects))


TEST_SUITE: list[TestSpec] = [
    _t("tests/test_orders.py::test_create_order", "api/orders", duration=0.8),
    _t("tests/test_orders.py::test_create_order_totals", "api/orders", duration=0.6,
       detects=["unit_test_regression"]),
    _t("tests/test_orders.py::test_list_orders_pagination", "api/orders", duration=0.9),
    _t("tests/test_orders.py::test_order_status_transitions", "api/orders", duration=1.1),
    _t("tests/test_orders.py::test_cancel_order", "api/orders", duration=0.7),
    _t("tests/test_serializers.py::test_order_serializer_roundtrip", "api/orders", duration=0.4),
    _t("tests/test_checkout.py::test_checkout_happy_path", "api/checkout",
       ["api/checkout", "integrations/payments", "api/orders"], 2.4, "integration"),
    _t("tests/test_checkout.py::test_checkout_empty_cart", "api/checkout", duration=0.5),
    _t("tests/test_checkout.py::test_checkout_applies_discount", "api/checkout", duration=0.9),
    _t("tests/test_payments_client.py::test_charge_success", "integrations/payments", duration=0.8),
    _t("tests/test_payments_client.py::test_charge_declined", "integrations/payments", duration=0.7),
    _t("tests/test_payments_client.py::test_retry_policy", "integrations/payments", duration=1.6),
    _t("tests/test_repository.py::test_insert_and_fetch", "db/repository", duration=1.2),
    _t("tests/test_repository.py::test_search_by_customer", "db/repository", duration=1.4),
    _t("tests/test_repository.py::test_soft_delete", "db/repository", duration=0.9),
    _t("tests/test_migrations.py::test_migrations_apply_cleanly", "db/repository", duration=4.5,
       kind="integration"),
    _t("tests/test_db.py::test_session_commit_rollback", "db/session", duration=1.0),
    _t("tests/test_db.py::test_session_scope_closes", "db/session", duration=0.6),
    _t("tests/test_cache.py::test_cache_hit_miss", "cache", duration=0.3),
    _t("tests/test_cache.py::test_cache_ttl_expiry", "cache", duration=1.3),
    _t("tests/test_auth.py::test_token_issue_and_verify", "auth", duration=0.7),
    _t("tests/test_auth.py::test_token_expired_rejected", "auth", duration=0.5),
    _t("tests/test_auth.py::test_tls_client_context", "auth", duration=0.9),
    _t("tests/test_config.py::test_defaults_load", "config", duration=0.3),
    _t("tests/test_config.py::test_env_overrides", "config", duration=0.4),
    _t("tests/test_health.py::test_liveness", "api/health", duration=0.2, kind="smoke"),
    _t("tests/test_health.py::test_readiness_checks_db", "api/health",
       ["api/health", "db/session"], 0.6, "smoke"),
    _t("tests/test_e2e.py::test_full_order_flow", "api/checkout",
       ["api/orders", "api/checkout", "integrations/payments", "db/repository", "auth"], 9.5, "e2e"),
    _t("tests/test_e2e.py::test_concurrent_orders", "api/orders",
       ["api/orders", "db/session", "db/repository"], 8.0, "e2e"),
    _t("tests/test_e2e.py::test_auth_flow", "auth", ["auth", "api/orders"], 5.5, "e2e"),
]


@dataclass(frozen=True)
class ActionSpec:
    type: str
    title: str
    risk: str
    description: str
    allowlisted: bool = False


ACTIONS: dict[str, ActionSpec] = {a.type: a for a in [
    ActionSpec("rerun_job", "Re-run failed job", "low",
               "Re-run the failed CI job or health probe.", True),
    ActionSpec("restart_container", "Restart service container", "low",
               "Restart the affected application container and wait for readiness.", True),
    ActionSpec("restart_dependency", "Restart dependency container", "low",
               "Restart the failing downstream dependency (e.g. payments-gateway).", True),
    ActionSpec("clear_cache", "Clear approved cache", "low",
               "Flush the in-process response cache (allowlisted namespaces only).", True),
    ActionSpec("rollback_deployment", "Roll back to last stable image", "medium",
               "Redeploy the most recent deployment marked stable."),
    ActionSpec("revert_config", "Revert configuration change", "medium",
               "Restore the last known-good non-critical configuration values."),
    ActionSpec("increase_pool_size", "Increase DB connection pool", "medium",
               "Raise the SQLAlchemy pool size/overflow for the service."),
    ActionSpec("apply_db_index", "Apply database index migration", "high",
               "Run a schema migration adding a missing index. Database change."),
    ActionSpec("rotate_certificate", "Rotate TLS certificate", "high",
               "Install a renewed client certificate. Security-sensitive change."),
    ActionSpec("escalate", "Escalate to on-call engineer", "low",
               "Hand the incident to a human with the collected evidence.", True),
]}


@dataclass(frozen=True)
class FaultSpec:
    id: str
    title: str
    description: str
    alert: str
    severity: str
    root_cause: str
    component: str
    module: str
    fix_actions: tuple[str, ...]
    missed_test: str
    distinctive_logs: tuple[str, ...]
    generic_logs: tuple[str, ...]
    metrics: dict[str, tuple[float, float]]  # name -> (baseline, faulty)
    trace: dict
    commit: dict | None = None
    deploy_related: bool = False
    tags: tuple[str, ...] = field(default=())


BASE_METRICS = {
    "http_error_rate": 0.004,
    "http_p95_latency_ms": 180.0,
    "cpu_usage": 0.32,
    "memory_mb": 210.0,
    "db_connections_in_use": 3.0,
    "upstream_error_rate": 0.002,
    "container_restarts": 0.0,
}

FAULTS: dict[str, FaultSpec] = {f.id: f for f in [
    FaultSpec(
        id="invalid_config",
        title="Invalid configuration value",
        description="A config change sets PAYMENT_TIMEOUT_MS to '3s', which fails integer parsing at startup.",
        alert="OrdersHighErrorRate",
        severity="critical",
        root_cause="Invalid configuration: PAYMENT_TIMEOUT_MS is not an integer, so settings fail to load "
                   "and workers crash on start.",
        component="config/settings.yaml",
        module="config",
        fix_actions=("revert_config", "rollback_deployment"),
        missed_test="tests/test_config.py::test_settings_schema_validation",
        distinctive_logs=(
            "ValueError: invalid literal for int() with base 10: '3s'",
            "settings: failed to parse PAYMENT_TIMEOUT_MS from config/settings.yaml",
            "pydantic ValidationError: payment_timeout_ms value is not a valid integer",
        ),
        generic_logs=(
            "worker exited with code 3, restarting",
            "HTTP 500 Internal Server Error on POST /checkout",
            "readiness probe failed: config not loaded",
        ),
        metrics={"http_error_rate": (0.004, 0.58), "container_restarts": (0, 6), "cpu_usage": (0.32, 0.18)},
        trace={"root": "POST /checkout", "slow_span": None, "error_span": "settings.load", "duration_ms": 35},
        commit={"message": "chore: tune payment timeout", "files": ["config/settings.yaml"]},
        tags=("config", "startup", "crashloop"),
    ),
    FaultSpec(
        id="dependency_failure",
        title="Failing downstream dependency",
        description="The payments-gateway dependency stops accepting connections.",
        alert="UpstreamDependencyErrors",
        severity="critical",
        root_cause="Downstream dependency payments-gateway is unavailable (connection refused / 503), "
                   "so checkout requests fail.",
        component="payments-gateway",
        module="integrations/payments",
        fix_actions=("restart_dependency",),
        missed_test="tests/test_payments_client.py::test_gateway_unavailable_fallback",
        distinctive_logs=(
            "httpx.ConnectError: [Errno 111] Connection refused (payments-gateway:8080)",
            "upstream payments-gateway returned 503 Service Unavailable",
            "circuit payments-gateway half-open: probe failed",
        ),
        generic_logs=(
            "HTTP 502 Bad Gateway on POST /checkout",
            "request to upstream failed after 3 retries",
            "checkout failed: payment step error",
        ),
        metrics={"upstream_error_rate": (0.002, 0.91), "http_error_rate": (0.004, 0.37),
                 "http_p95_latency_ms": (180, 950)},
        trace={"root": "POST /checkout", "slow_span": "payments.charge", "error_span": "payments.charge",
               "duration_ms": 3050},
        tags=("dependency", "network", "upstream"),
    ),
    FaultSpec(
        id="app_exception",
        title="Application exception after deployment",
        description="A new release serializes orders without a currency field, raising KeyError.",
        alert="OrdersHighErrorRate",
        severity="critical",
        root_cause="Regression in the latest deployment: OrderSerializer reads order['currency'], which is "
                   "missing for legacy orders, raising KeyError on GET /orders.",
        component="app/orders/serializers.py",
        module="api/orders",
        fix_actions=("rollback_deployment",),
        missed_test="tests/test_orders.py::test_order_without_currency_defaults",
        distinctive_logs=(
            "KeyError: 'currency' in app/orders/serializers.py line 42, in to_dict",
            "Traceback (most recent call last): OrderSerializer.to_dict",
            "unhandled exception in GET /orders/{id} after release v{version}",
        ),
        generic_logs=(
            "HTTP 500 Internal Server Error on GET /orders/1182",
            "error rate above SLO for /orders",
            "exception count increased since last deploy",
        ),
        metrics={"http_error_rate": (0.004, 0.24), "http_p95_latency_ms": (180, 240)},
        trace={"root": "GET /orders/{id}", "slow_span": None, "error_span": "OrderSerializer.to_dict",
               "duration_ms": 41},
        commit={"message": "feat(orders): add multi-currency display", "files": ["app/orders/serializers.py"]},
        deploy_related=True,
        tags=("regression", "exception", "deploy"),
    ),
    FaultSpec(
        id="slow_query",
        title="Slow query from missing index",
        description="A new search filter scans the orders table because customer_email has no index.",
        alert="OrdersHighLatency",
        severity="warning",
        root_cause="Missing database index on orders.customer_email causes full table scans; "
                   "the search query takes >2s and drives p95 latency up.",
        component="db/repository.search_orders",
        module="db/repository",
        fix_actions=("apply_db_index",),
        missed_test="tests/test_repository.py::test_search_query_uses_index",
        distinctive_logs=(
            "slow query (2412 ms): SELECT * FROM orders WHERE customer_email = $1",
            "EXPLAIN: Seq Scan on orders (rows=1840221) Filter: customer_email",
            "postgres: duration 2398 ms statement: SELECT ... customer_email",
        ),
        generic_logs=(
            "request GET /orders/search took 2.6s",
            "latency SLO breach on /orders/search",
            "database response time elevated",
        ),
        metrics={"http_p95_latency_ms": (180, 2650), "cpu_usage": (0.32, 0.71),
                 "db_connections_in_use": (3, 7)},
        trace={"root": "GET /orders/search", "slow_span": "db.query orders", "error_span": None,
               "duration_ms": 2580},
        commit={"message": "feat(search): filter orders by customer email", "files": ["app/db/repository.py"]},
        tags=("database", "latency", "index"),
    ),
    FaultSpec(
        id="memory_leak",
        title="Memory pressure from unbounded cache",
        description="The response cache never evicts entries, so memory grows until the container is OOM-killed.",
        alert="OrdersMemoryPressure",
        severity="warning",
        root_cause="Unbounded in-process LRU cache (maxsize=None) grows without eviction, causing memory "
                   "pressure and OOM kills.",
        component="app/cache/lru.py",
        module="cache",
        fix_actions=("restart_container", "clear_cache"),
        missed_test="tests/test_cache.py::test_cache_eviction_bound",
        distinctive_logs=(
            "cache stats: entries=1843311 maxsize=None evictions=0",
            "container orders-service OOMKilled (memory limit 512Mi)",
            "MemoryError while allocating response cache entry",
        ),
        generic_logs=(
            "GC pause 840ms",
            "worker restarted unexpectedly",
            "latency increased on GET /orders",
        ),
        metrics={"memory_mb": (210, 498), "container_restarts": (0, 2), "http_p95_latency_ms": (180, 640)},
        trace={"root": "GET /orders", "slow_span": "cache.get", "error_span": None, "duration_ms": 620},
        commit={"message": "perf: cache order responses in-process", "files": ["app/cache/lru.py"]},
        tags=("memory", "resource", "cache"),
    ),
    FaultSpec(
        id="db_pool_exhaustion",
        title="Database connection pool exhaustion",
        description="Traffic spike exhausts the 5-connection SQLAlchemy pool; requests wait for connections.",
        alert="OrdersHighLatency",
        severity="warning",
        root_cause="SQLAlchemy connection pool (size=5, overflow=0) is exhausted under load; requests block "
                   "waiting for a connection and time out.",
        component="app/db/session.py",
        module="db/session",
        fix_actions=("increase_pool_size",),
        missed_test="tests/test_db.py::test_pool_under_concurrency",
        distinctive_logs=(
            "sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out",
            "waiting for DB connection: pool checked_out=5 overflow=0",
            "db pool saturation 100% for 120s",
        ),
        generic_logs=(
            "request GET /orders took 3.1s",
            "database response time elevated",
            "HTTP 504 Gateway Timeout on GET /orders",
        ),
        metrics={"http_p95_latency_ms": (180, 3100), "db_connections_in_use": (3, 5),
                 "http_error_rate": (0.004, 0.09)},
        trace={"root": "GET /orders", "slow_span": "db.connect", "error_span": None, "duration_ms": 3050},
        tags=("database", "latency", "pool"),
    ),
    FaultSpec(
        id="cert_expired",
        title="Expired TLS client certificate",
        description="The mTLS client certificate used to call the auth provider expired.",
        alert="UpstreamDependencyErrors",
        severity="critical",
        root_cause="The client TLS certificate for the auth provider has expired, so every token "
                   "introspection call fails the TLS handshake.",
        component="app/auth/tls.py",
        module="auth",
        fix_actions=("rotate_certificate",),
        missed_test="tests/test_auth.py::test_certificate_expiry_check",
        distinctive_logs=(
            "ssl.SSLCertVerificationError: certificate verify failed: certificate has expired",
            "TLS handshake with auth-provider:443 failed: client certificate expired (notAfter passed)",
            "x509: certificate has expired or is not yet valid",
        ),
        generic_logs=(
            "request to upstream failed after 3 retries",
            "HTTP 401 Unauthorized on GET /orders",
            "upstream auth-provider connection error",
        ),
        metrics={"upstream_error_rate": (0.002, 0.97), "http_error_rate": (0.004, 0.66)},
        trace={"root": "GET /orders", "slow_span": None, "error_span": "auth.introspect", "duration_ms": 120},
        tags=("security", "tls", "auth"),
    ),
]}


# How each scenario reaches the environment in the Fault Lab and in experiments: shipped inside a commit
# through the pipeline (so CI could catch a recurrence) or injected at runtime (config/infra/dependency).
SHIP_VIA_PIPELINE = {"app_exception", "slow_query", "memory_leak"}


@dataclass(frozen=True)
class CIDefect:
    id: str
    title: str
    stage: str  # build | test
    files: tuple[str, ...]
    log: str
    explanation: str
    suggestion: str


CI_DEFECTS: dict[str, CIDefect] = {d.id: d for d in [
    CIDefect(
        "build_dependency_conflict", "Dependency conflict", "build", ("requirements.txt",),
        "ERROR: Cannot install pydantic==1.10.13 and fastapi==0.115.0 because these package versions "
        "have conflicting dependencies.\nThe conflict is caused by: fastapi 0.115.0 depends on "
        "pydantic>=2.7.0",
        "The build failed while resolving dependencies: the commit pins pydantic 1.10.x, but "
        "FastAPI 0.115 requires pydantic >= 2.7.",
        "Unpin pydantic or bump it to >=2.7,<3 in requirements.txt and re-run the pipeline.",
    ),
    CIDefect(
        "syntax_error", "Syntax error", "build", ("app/orders/routes.py",),
        '  File "app/orders/routes.py", line 57\n    def list_orders(limit: int = 50\n'
        "                                   ^\nSyntaxError: '(' was never closed",
        "Compilation failed: a function signature in app/orders/routes.py line 57 is missing its "
        "closing parenthesis.",
        "Close the parenthesis on line 57 of app/orders/routes.py.",
    ),
    CIDefect(
        "unit_test_regression", "Order total regression", "test", ("app/orders/routes.py",),
        "FAILED tests/test_orders.py::test_create_order_totals - AssertionError: assert 107.0 == 117.0\n"
        "  where 107.0 = order.total  (tax applied before discount)",
        "test_create_order_totals failed: the order total is now computed with tax applied before "
        "discount, producing 107.0 instead of 117.0.",
        "Restore the original order of operations in the totals calculation (discount, then tax).",
    ),
]}
