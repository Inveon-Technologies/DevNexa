"""Versions and backups, live issue capture, fix branches with test URLs, and per-app pipeline settings."""

import json

from app.agents import deploy, fixer
from app.db import session_scope
from app.models import AppDeployment, AppIssue
from app.services import github, issues, releases

from test_deploy import fake  # noqa: F401  (fixture)

FILES = {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
         "index.html": "<html><head><title>x</title></head><body><main id='cart'></main></body></html>"}


def deploy_app(client, app, repo=None):
    r = client.post("/api/apps/deploy", json={"repo": repo or f"vaibhav/{app}", "branch": "main", "app": app})
    assert r.status_code == 201, r.text
    return client.get(f"/api/apps/deployments/{r.json()['id']}").json()


def rels(client, app):
    return client.get(f"/api/live/apps/{app}/releases").json()


# ---------------------------------------------------------------- versions
def test_each_deploy_is_a_new_version_and_old_ones_are_backups(client, fake):
    ex = fake(files=FILES)
    deploy_app(client, "ver-app")
    d2 = deploy_app(client, "ver-app")
    assert d2["status"] == "running"
    r = rels(client, "ver-app")
    assert [(x["label"], x["status"]) for x in r] == [("v2", "live"), ("v1", "backup")]
    assert ["docker", "tag", d2["image"], "devnexa-app/ver-app:v2"] in ex.calls

    # roll back to v1: re-uses its image (no rebuild) and makes v1 live again without a new version
    res = client.post(f"/api/live/apps/ver-app/releases/{r[1]['id']}/rollback", json={"actor": "vaibhav"})
    assert res.status_code == 201, res.text
    dep = client.get(f"/api/apps/deployments/{res.json()['id']}").json()
    assert dep["status"] == "running" and dep["image"] == "devnexa-app/ver-app:v1"
    assert [(x["label"], x["status"]) for x in rels(client, "ver-app")] == [("v2", "backup"), ("v1", "live")]


def test_retention_and_switching_backups_off(client, fake):
    ex = fake(files=FILES)
    deploy_app(client, "keep-app")
    client.put("/api/live/apps/keep-app/settings", json={"keep_versions": 1})
    deploy_app(client, "keep-app")
    deploy_app(client, "keep-app")
    assert [x["status"] for x in rels(client, "keep-app")] == ["live", "backup", "removed"]
    assert ["docker", "rmi", "devnexa-app/keep-app:v1"] in ex.calls

    client.put("/api/live/apps/keep-app/settings", json={"steps": {"auto_backup": False}})
    deploy_app(client, "keep-app")
    assert [x["status"] for x in rels(client, "keep-app")] == ["live", "removed", "removed", "removed"]


def test_backup_schedules_can_be_added_run_and_removed(client, fake):
    ex = fake(files=FILES)
    deploy_app(client, "bak-app")
    s = client.post("/api/live/apps/bak-app/backup-schedules", json={"every_hours": 6, "keep": 2}).json()
    assert client.get("/api/live/apps/bak-app").json()["settings"]["backup_schedules"][0]["id"] == s["id"]
    releases.run_due_backups()
    snap = [r for r in rels(client, "bak-app") if r["kind"] == "snapshot"]
    assert len(snap) == 1 and snap[0]["source"] == "schedule"
    assert any(c[:2] == ["docker", "commit"] for c in ex.calls)
    releases.run_due_backups()  # not due again for 6 hours
    assert len([r for r in rels(client, "bak-app") if r["kind"] == "snapshot"]) == 1

    assert client.delete(f"/api/live/apps/bak-app/backup-schedules/{s['id']}").status_code == 200
    assert client.get("/api/live/apps/bak-app").json()["settings"]["backup_schedules"] == []
    assert client.delete(f"/api/live/apps/bak-app/backup-schedules/{s['id']}").status_code == 404


# ------------------------------------------------------------ load balancer
def test_load_balancer_runs_replicas_and_rotates_traffic(client, fake):
    ex = fake(files=FILES)
    d = deploy_app(client, "lb-app")
    r = client.put("/api/live/apps/lb-app/settings", json={"steps": {"load_balancer": True}, "replicas": 3})
    assert r.json()["steps"]["load_balancer"] is True
    lb = client.get("/api/live/apps/lb-app").json()["load_balancer"]
    assert len(lb["upstreams"]) == 3 and len(lb["replicas"]) == 2
    assert any(c[:2] == ["docker", "run"] and f"{d['container']}-r2" in c for c in ex.calls)
    seen = {client.get("/api/apps/route", headers={"x-forwarded-host": "lb-app.devnexa.space"})
            .headers["x-devnexa-upstream"] for _ in range(6)}
    assert len(seen) == 3

    client.put("/api/live/apps/lb-app/settings", json={"steps": {"load_balancer": False}})
    assert client.get("/api/live/apps/lb-app").json()["load_balancer"]["replicas"] == []
    assert ["docker", "rm", "-f", f"{d['container']}-r3"] in ex.calls


# ------------------------------------------------------------ issue capture
def test_build_injects_the_issue_capture_script(tmp_path):
    (tmp_path / "index.html").write_text("<html><head></head><body></body></html>")
    assert issues.inject_agent(tmp_path, "shop") == ["index.html"]
    text = (tmp_path / "index.html").read_text()
    assert '/__devnexa/agent.js" data-app="shop"' in text
    assert issues.inject_agent(tmp_path, "shop") == []  # idempotent


def test_browser_reports_become_grouped_issues(client, fake):
    fake(files=FILES)
    deploy_app(client, "cap-app")
    assert "sendBeacon" in client.get("/__devnexa/agent.js").text
    host = {"host": "cap-app.devnexa.space"}
    rep = {"kind": "js_error", "message": "TypeError: Cannot read properties of undefined (reading 'total') at 12:7",
           "page": "/checkout", "stack": "at Cart (/assets/Checkout-3f2a9c1d.js:1:200)"}
    for line in (12, 40):
        r = client.post("/__devnexa/report", content=json.dumps(rep | {"message": rep["message"].replace("12", str(line))}),
                        headers=host)
        assert r.status_code == 204
    client.post("/__devnexa/report", headers=host, content='{"kind":"blank_section","message":"Section has no content",'
                '"page":"/","selector":"main#cart","section":"main#cart"}')
    client.post("/__devnexa/report", headers={"host": "nope.devnexa.space"}, content='{"kind":"js_error","message":"x"}')
    got = client.get("/api/live/apps/cap-app/issues").json()
    js = next(i for i in got if i["kind"] == "js_error")
    assert js["count"] == 2 and js["page"] == "/checkout"
    blank = next(i for i in got if i["kind"] == "blank_section")
    assert blank["severity"] == "warning" and blank["section"] == "main#cart"
    with session_scope() as db:
        assert not db.query(AppIssue).filter(AppIssue.app == "nope").count()


def test_server_logs_become_issues():
    with session_scope() as db:
        found = issues.from_logs(db, "log-app", 1, "\n".join([
            'INFO: 10.0.0.1 - "GET /api/orders/42 HTTP/1.1" 500 Internal Server Error',
            "Traceback (most recent call last):",
            '  File "/app/main.py", line 9, in orders',
            "KeyError: 'total'",
        ]))
        assert sorted(i.message for i in found) == ["GET /api/orders/42 returned 500", "KeyError: 'total'"]
        assert found[0].page == "/api/orders/42"


# --------------------------------------------------- fix branch + test URL
def _fake_github(monkeypatch, calls):
    monkeypatch.setattr(github, "commit_files", lambda *a, **k: calls.append(("commit", a[3], dict(a[4]))))
    monkeypatch.setattr(github, "find_or_open_pr", lambda *a, **k: calls.append(("pr", a[2])) or
                        {"number": 7, "url": "https://github.com/vaibhav/fix-app/pull/7"})
    monkeypatch.setattr(github, "merge_pr", lambda *a, **k: calls.append(("merge", a[2])) or "f00d")
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: {"merged": False, "mergeable": True,
                                                             "mergeable_state": "clean", "head_sha": "h"})
    monkeypatch.setattr(github, "close_pr", lambda *a, **k: calls.append(("close", a[2])))
    monkeypatch.setattr(github, "delete_branch", lambda *a, **k: calls.append(("delete", a[2])))


def test_issue_fix_gets_a_test_url_and_merges_on_approval(client, fake, monkeypatch):
    fake(files=FILES)
    calls = []
    _fake_github(monkeypatch, calls)
    monkeypatch.setattr(fixer, "propose", lambda issue, root, hint=None: {
        "title": "Handle empty cart", "root_cause": "cart.total is read before the cart loads",
        "suggestion": "Guard the total", "confidence": 0.8, "provider": "test",
        "patch": {"main.py": (root / "main.py").read_text() + "# fixed\n"}})
    deploy_app(client, "fix-app")
    with session_scope() as db:
        issue, _ = issues.record(db, "fix-app", "js_error", "TypeError: total of undefined", "/checkout")
        iid = issue.id

    r = client.post(f"/api/live/apps/fix-app/issues/{iid}/fix", json={"actor": "vaibhav", "hint": "cart page"})
    assert r.status_code == 202, r.text
    assert calls[0] == ("commit", f"devnexa/issue-fix-app-{iid}", {"main.py": FILES["main.py"] + "# fixed\n"})
    pv = client.get("/api/live/apps/fix-app/previews").json()[0]
    assert pv["preview_app"] == "fix-app-pr-7" and pv["status"] == "ready"
    assert pv["url"] == "https://fix-app-pr-7.devnexa.space" and pv["pr_number"] == 7
    issue = next(i for i in client.get("/api/live/apps/fix-app/issues").json() if i["id"] == iid)
    assert issue["status"] == "in_preview" and issue["fix"]["pr_url"].endswith("/pull/7")
    # test URLs do not create versions and are not listed as apps
    assert [x["label"] for x in rels(client, "fix-app")] == ["v1"]
    assert "fix-app-pr-7" not in [a["app"] for a in client.get("/api/apps").json()]

    res = client.post(f"/api/live/previews/{pv['id']}/approve", json={"actor": "vaibhav"})
    assert res.status_code == 200, res.text
    assert ("merge", 7) in calls and ("delete", f"devnexa/issue-fix-app-{iid}") in calls
    assert client.get("/api/live/apps/fix-app/previews").json()[0]["status"] == "merged"
    r = rels(client, "fix-app")
    assert [(x["label"], x["status"], x["source"]) for x in r][:2] == [("v2", "live", "fix-merge"), ("v1", "backup", "deploy")]
    with session_scope() as db:
        assert db.get(AppIssue, iid).status == "resolved"
        assert {d.status for d in db.query(AppDeployment).filter(AppDeployment.app == "fix-app-pr-7")} == {"stopped"}
        # the bug comes back after the fix: the issue reopens
        issues.record(db, "fix-app", "js_error", "TypeError: total of undefined", "/checkout")
        assert db.get(AppIssue, iid).status == "open" and db.get(AppIssue, iid).reopened == 1


def test_rejecting_a_fix_closes_the_pr_and_removes_the_test_url(client, fake, monkeypatch):
    fake(files=FILES)
    calls = []
    _fake_github(monkeypatch, calls)
    monkeypatch.setattr(fixer, "propose", lambda issue, root, hint=None: {
        "title": "t", "root_cause": "r", "suggestion": "s", "confidence": 0.5, "provider": "test",
        "patch": {"main.py": "print(1)\n"}})
    deploy_app(client, "rej-app")
    with session_scope() as db:
        iid = issues.record(db, "rej-app", "api_error", "GET /api/x returned 500", "/")[0].id
    client.post(f"/api/live/apps/rej-app/issues/{iid}/fix", json={})
    pv = client.get("/api/live/apps/rej-app/previews").json()[0]
    assert client.post(f"/api/live/previews/{pv['id']}/reject", json={"reason": "breaks login"}).status_code == 200
    assert ("close", 7) in calls and ("merge", 7) not in calls
    assert client.get("/api/live/apps/rej-app/previews").json()[0]["status"] == "rejected"
    with session_scope() as db:
        assert db.get(AppIssue, iid).status == "open"


def test_no_patch_without_llm_is_reported_honestly(client, fake, monkeypatch):
    fake(files=FILES)
    _fake_github(monkeypatch, [])
    deploy_app(client, "np-app")
    with session_scope() as db:
        iid = issues.record(db, "np-app", "user_report", "Prices are wrong", "/shop", "div.price", "main")[0].id
    client.post(f"/api/live/apps/np-app/issues/{iid}/fix", json={})
    issue = next(i for i in client.get("/api/live/apps/np-app/issues").json() if i["id"] == iid)
    assert issue["status"] == "open" and issue["fix"]["status"] == "no_patch"
    assert "DEVNEXA_LLM_PROVIDER" in issue["fix"]["diagnosis"]["suggestion"]


def test_heuristic_patch_for_missing_python_module(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    prop = fixer.propose({"kind": "server_error", "message": "ModuleNotFoundError: No module named 'yaml'",
                          "page": "/", "stack": "Traceback\nModuleNotFoundError: No module named 'yaml'"}, tmp_path)
    assert prop["patch"] == {"requirements.txt": "fastapi\npyyaml\n"}


def test_repeated_issue_triggers_auto_fix_once(client, fake, monkeypatch):
    fake(files=FILES)
    started = []
    monkeypatch.setattr(fixer, "start_fix", lambda iid, actor, hint=None: started.append((iid, actor)))
    deploy_app(client, "auto-app")
    body = '{"kind":"js_error","message":"ReferenceError: price is not defined","page":"/"}'
    for _ in range(4):
        issues._hits.clear()
        client.post("/__devnexa/report", content=body, headers={"host": "auto-app.devnexa.space"})
    assert len(started) == 1 and started[0][1] == "fix-agent"

    client.put("/api/live/apps/auto-app/settings", json={"steps": {"auto_fix": False}})
    body2 = body.replace("price", "qty")
    for _ in range(4):
        client.post("/__devnexa/report", content=body2, headers={"host": "auto-app.devnexa.space"})
    assert len(started) == 1


def test_push_to_a_fix_branch_redeploys_its_test_url(client, fake, monkeypatch):
    from app.api.webhooks import redeploy_pushed_apps

    fake(files=FILES)
    _fake_github(monkeypatch, [])
    monkeypatch.setattr(github, "find_or_open_pr", lambda *a, **k: {"number": 9, "url": "u"})
    deploy_app(client, "br-app")
    r = client.post("/api/live/apps/br-app/previews", json={"branch": "feature/new-cart"})
    assert r.status_code == 201, r.text
    assert r.json()["preview_app"] == "br-app-pr-9" and r.json()["status"] == "ready"
    with session_scope() as db:
        ids = redeploy_pushed_apps(db, "vaibhav/br-app", "feature/new-cart")
        dep = db.get(AppDeployment, ids[0])
        assert dep.app == "br-app-pr-9" and dep.options["preview_of"] == "br-app"
    assert client.post("/api/live/apps/br-app/previews", json={"branch": "main"}).status_code == 400


def test_live_apps_overview(client, fake):
    fake(files=FILES)
    deploy_app(client, "ov-app")
    apps = {a["app"]: a for a in client.get("/api/live/apps").json()}
    assert apps["ov-app"]["version"] == "v1" and apps["ov-app"]["live"]["status"] == "running"
    ov = client.get("/api/live/apps/ov-app").json()
    assert {s["key"] for s in ov["steps"]} >= {"auto_backup", "load_balancer", "issue_capture", "auto_fix"}
    assert deploy.executor  # fixture swapped it


def test_pointed_change_from_a_signed_link_goes_to_the_ai_at_once(client, fake, monkeypatch):
    fake(files=FILES)
    started = []
    monkeypatch.setattr(fixer, "start_fix", lambda iid, actor, hint=None: started.append((iid, actor, hint)))
    deploy_app(client, "point-app")
    link = client.post("/api/live/apps/point-app/report-link").json()["url"]
    assert link.startswith("https://point-app.devnexa.space/?devnexa-report=")
    token = link.split("devnexa-report=")[1]
    host = {"host": "point-app.devnexa.space"}
    change = {"kind": "user_report", "message": "Make this heading say Contact us", "page": "/", "selector": "h1",
              "text": "Get in touch"}
    issues._hits.clear()
    client.post("/__devnexa/report", content=json.dumps(change), headers=host)  # a visitor: recorded only
    assert started == []
    issues._hits.clear()
    client.post("/__devnexa/report", content=json.dumps(change | {"token": token}), headers=host)
    assert len(started) == 1 and started[0][2] == "Make this heading say Contact us"
    issues._hits.clear()  # a token for another app does nothing
    other = client.post("/__devnexa/report", content=json.dumps(change | {"message": "x", "token": token}),
                        headers={"host": "auto-app.devnexa.space"})
    assert other.status_code == 204 and len(started) == 1


def test_agent_tokens_use_the_pointed_section_text():
    toks = fixer._tokens({"kind": "user_report", "message": "make it blue", "page": "/",
                          "sample": {"text": "Contact Vaibhav today", "html": '<h2 id="contact-title">x</h2>'}})
    assert "contact" in toks and "contact-title" in toks
