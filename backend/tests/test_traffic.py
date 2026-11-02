import json
import time

from app.services import traffic
from app.services.traffic import Hit, TrafficStore, mask_ip, parse_caddy_line


def caddy_line(host="shop.devnexa.space", uri="/cart?x=1", status=200, duration=0.05, ip="203.0.113.7", ts=None):
    return json.dumps({
        "level": "info", "ts": ts or time.time(), "logger": "http.log.access", "msg": "handled request",
        "request": {"remote_ip": "172.18.0.1", "client_ip": ip, "proto": "HTTP/2.0", "method": "GET", "host": host,
                    "uri": uri, "headers": {"User-Agent": ["Mozilla/5.0"]}},
        "duration": duration, "size": 10, "status": status,
    })


def test_parse_caddy_line():
    hit = parse_caddy_line(caddy_line(), "devnexa.space")
    assert hit.app == "shop" and hit.path == "/cart" and hit.status == 200
    assert round(hit.ms) == 50 and hit.client == "203.0.113.x"
    assert parse_caddy_line(caddy_line(host="devnexa.space"), "devnexa.space") is None
    assert parse_caddy_line("not json", "devnexa.space") is None
    assert parse_caddy_line(json.dumps({"msg": "startup"}), "devnexa.space") is None


def test_mask_ip():
    assert mask_ip("10.1.2.3") == "10.1.2.x"
    assert mask_ip("2001:db8:abcd:12::1") == "2001:db8:abcd:…"
    assert mask_ip("") == "unknown"


def test_snapshot_aggregates_traffic_and_problems():
    s = TrafficStore()
    s.source = "caddy"
    now = time.time()
    for i in range(20):
        s.add(Hit(now - i, "shop", "GET", "/", 200, 40, f"v{i % 5}", "1.2.3.x"))
    for _ in range(3):
        s.add(Hit(now, "shop", "POST", "/checkout", 500, 900, "v1", "1.2.3.x"))
    s.add(Hit(now, "api", "GET", "/missing", 404, 5, "v9", "1.2.3.x"))
    snap = s.snapshot(minutes=10, now=now)
    assert snap["summary"]["requests"] == 24
    assert snap["summary"]["errors"] == 3 and snap["summary"]["client_errors"] == 1
    assert snap["summary"]["visitors"] == 6
    assert snap["problems"][0] == {**snap["problems"][0], "path": "/checkout", "status": 500, "count": 3,
                                   "severity": "error"}
    assert snap["problems"][1]["status"] == 404
    shop = next(a for a in snap["apps"] if a["app"] == "shop")
    assert shop["requests"] == 23 and shop["errors"] == 3 and shop["visitors"] == 5
    assert len(snap["series"]) == 10

    only = s.snapshot(minutes=10, app="api", now=now)
    assert only["summary"]["requests"] == 1 and [a["app"] for a in only["apps"]] == ["api"]
    errs = s.snapshot(minutes=10, errors_only=True, now=now)
    assert all(r["status"] >= 400 for r in errs["recent"]) and len(errs["recent"]) == 4


def test_old_buckets_are_dropped():
    s = TrafficStore()
    now = time.time()
    s.add(Hit(now - 3 * 3600, "shop", "GET", "/", 200, 1, "a", "x"))
    s.add(Hit(now, "shop", "GET", "/", 200, 1, "a", "x"))
    assert len(s._buckets) == 1


def test_traffic_endpoint(client):
    traffic.store.reset()
    traffic.store.add(Hit(time.time(), "shop", "GET", "/boom", 502, 12, "v", "1.2.3.x"))
    r = client.get("/api/traffic?minutes=5")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["errors"] == 1 and body["problems"][0]["path"] == "/boom"
    assert body["recent"][0]["client"] == "1.2.3.x"
    traffic.store.reset()
