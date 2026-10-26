"""orders-service: the sample microservice DevNexa builds, deploys, monitors and repairs.

Every request emits Prometheus metrics, structured logs (collected by Loki) and OpenTelemetry spans
(exported to Jaeger when OTEL_EXPORTER_OTLP_ENDPOINT is set). Admin endpoints let DevNexa inject the
controlled faults used in experiments and execute remediation actions.
"""

import logging
import os
import random
import threading
import time
from contextlib import contextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from .faults import FaultState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s orders-service %(message)s")
log = logging.getLogger("orders")

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Request latency", ["path"],
                    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5))
UPSTREAM = Gauge("orders_upstream_error_ratio", "Share of failing upstream calls (payments, auth)")
DB_CONN = Gauge("orders_db_connections_in_use", "DB connections in use")
MEMORY = Gauge("orders_memory_mb", "Service memory in MB (includes simulated cache growth)")
VERSION = Gauge("orders_build_info", "Deployed version", ["version"])

state = FaultState()
app = FastAPI(title="orders-service (DevNexa sample app)")

# ---------------------------------------------------------------- tracing
tracer = None
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "orders-service"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(insecure=True)))
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer("orders-service")
    except Exception as e:  # pragma: no cover
        log.warning("tracing disabled: %s", e)


@contextmanager
def span(name: str):
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as sp:
        try:
            yield sp
        except Exception as e:
            sp.set_attribute("error", True)
            sp.record_exception(e)
            raise


# ------------------------------------------------------------- middleware
_cache: list[bytes] = []
_cache_mb = 0.0


@app.middleware("http")
async def observe(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/admin", "/metrics")):
        return await call_next(request)
    route = path if not path.startswith("/orders/") or path == "/orders/search" else "/orders/{id}"
    start = time.perf_counter()
    with span(f"{request.method} {route}"):
        try:
            if state.active("invalid_config") and path != "/health":
                log.error("ValueError: invalid literal for int() with base 10: '3s'")
                log.error("settings: failed to parse PAYMENT_TIMEOUT_MS from config/settings.yaml")
                resp = JSONResponse({"detail": "configuration error"}, status_code=500)
            else:
                resp = await call_next(request)
        except Exception as e:
            log.error("unhandled exception in %s %s: %r", request.method, route, e)
            resp = JSONResponse({"detail": "internal error"}, status_code=500)
    LATENCY.labels(route).observe(time.perf_counter() - start)
    REQUESTS.labels(request.method, route, str(resp.status_code)).inc()
    if resp.status_code >= 500:
        log.error("HTTP %s on %s %s", resp.status_code, request.method, path)
    return resp


def _db(op: str):
    with span(f"db.query orders"):
        if state.active("db_pool_exhaustion"):
            DB_CONN.set(5)
            time.sleep(3.0)
            log.error("sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, "
                      "connection timed out")
            raise HTTPException(504, "database pool exhausted")
        DB_CONN.set(random.randint(1, 3))
        time.sleep(random.uniform(0.005, 0.02))


def _auth():
    with span("auth.verify_token"):
        if state.active("cert_expired"):
            UPSTREAM.set(0.97)
            log.error("ssl.SSLCertVerificationError: certificate verify failed: certificate has expired")
            raise HTTPException(401, "auth provider unreachable")


def _cache_get():
    global _cache_mb
    with span("cache.get"):
        if state.active("memory_leak"):
            _cache_mb = min(_cache_mb + 2.5, 300)
            if _cache_mb > 240:
                log.error("cache stats: entries=%d maxsize=None evictions=0", int(_cache_mb * 3800))
            time.sleep(0.05 * _cache_mb / 100)
        else:
            _cache_mb = max(0.0, _cache_mb - 50)
        MEMORY.set(200 + _cache_mb)


ORDERS = {i: {"id": i, "customer_email": f"user{i}@example.com", "total": round(20 + i * 1.7, 2),
              "currency": "INR" if i % 7 else None} for i in range(1, 400)}


@app.get("/health")
def health():
    if state.active("invalid_config"):
        log.error("readiness probe failed: config not loaded")
        raise HTTPException(503, "config not loaded")
    return {"status": "ok", "version": state.state["version"]}


@app.get("/orders")
def list_orders(limit: int = 20):
    _auth()
    _cache_get()
    _db("list")
    return list(ORDERS.values())[:limit]


@app.get("/orders/search")
def search(email: str = "user1@example.com"):
    _auth()
    with span("db.query orders"):
        if state.active("slow_query"):
            time.sleep(2.4)
            log.warning("slow query (2412 ms): SELECT * FROM orders WHERE customer_email = $1")
    return [o for o in ORDERS.values() if o["customer_email"] == email]


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    _auth()
    _db("get")
    order = ORDERS.get(order_id) or ORDERS[1]
    with span("OrderSerializer.to_dict"):
        if state.active("app_exception") and order["currency"] is None:
            log.error("KeyError: 'currency' in app/orders/serializers.py line 42, in to_dict")
            raise KeyError("currency")
    return {**order, "currency": order["currency"] or "INR"}


class Checkout(BaseModel):
    order_id: int = 1


@app.post("/checkout")
def checkout(body: Checkout):
    _auth()
    with span("payments.charge"):
        if state.active("dependency_failure"):
            UPSTREAM.set(0.91)
            time.sleep(1.0)
            log.error("httpx.ConnectError: [Errno 111] Connection refused (payments-gateway:8080)")
            raise HTTPException(502, "payment gateway unavailable")
    if not state.active("cert_expired"):
        UPSTREAM.set(0.0)
    return {"order_id": body.order_id, "status": "paid"}


@app.get("/metrics")
def metrics():
    VERSION.clear()
    VERSION.labels(state.state["version"]).set(1)
    if not state.state["active_faults"]:
        UPSTREAM.set(0)
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ------------------------------------------------------------------ admin
class DeployBody(BaseModel):
    version: str
    image: str = ""
    commit_sha: str = ""
    defect: str | None = None


@app.get("/admin/state")
def admin_state():
    return state.snapshot()


@app.post("/admin/reset")
def admin_reset():
    state.reset()
    return state.snapshot()


@app.post("/admin/faults/{fault}")
def admin_inject(fault: str, via_deploy: bool = False):
    try:
        state.inject(fault, via_deploy)
    except KeyError:
        raise HTTPException(404, "unknown fault")
    log.warning("controlled fault injected: %s", fault)
    return state.snapshot()


@app.delete("/admin/faults")
def admin_clear():
    state.clear()
    return state.snapshot()


@app.post("/admin/deploy")
def admin_deploy(body: DeployBody):
    state.deploy(body.version, body.image, body.commit_sha, body.defect)
    return {"ok": True, "version": body.version}


@app.post("/admin/rollback")
def admin_rollback(body: DeployBody):
    removed = state.rollback(body.version, body.image, body.commit_sha)
    return {"ok": True, "version": body.version, "resolved_faults": removed,
            "output": f"rolled back to {body.version}"}


@app.post("/admin/actions/{action}")
def admin_action(action: str):
    global _cache_mb
    try:
        resolved = state.apply(action)
    except KeyError:
        raise HTTPException(404, "unknown action")
    if action in ("restart_container", "clear_cache"):
        _cache_mb = 0.0
    log.info("remediation action %s executed, resolved=%s", action, resolved)
    return {"ok": True, "action": action, "resolved_faults": resolved, "output": f"{action} executed"}


@app.get("/admin/metrics")
def admin_metrics():
    return {"memory_mb": 200 + _cache_mb}


# ------------------------------------------------------------- load generator
def _loadgen():  # pragma: no cover - background traffic for demos
    port = os.getenv("PORT", "8000")
    base = f"http://127.0.0.1:{port}"
    time.sleep(3)
    with httpx.Client(timeout=10) as c:
        while True:
            try:
                r = random.random()
                if r < 0.4:
                    c.get(f"{base}/orders")
                elif r < 0.65:
                    c.get(f"{base}/orders/{random.randint(1, 399)}")
                elif r < 0.8:
                    c.get(f"{base}/orders/search", params={"email": f"user{random.randint(1, 399)}@example.com"})
                else:
                    c.post(f"{base}/checkout", json={"order_id": random.randint(1, 399)})
            except httpx.HTTPError:
                pass
            time.sleep(float(os.getenv("LOADGEN_INTERVAL", "0.2")))


if os.getenv("LOADGEN", "false").lower() == "true":
    threading.Thread(target=_loadgen, daemon=True).start()
