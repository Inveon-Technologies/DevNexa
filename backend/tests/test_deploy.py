import json
from pathlib import Path

import pytest

from app.agents import deploy
from app.config import get_settings
from app.services import app_doctor, buildplan, github


class FakeExecutor:
    """Pretends to be git + docker. `files` is what the cloned repo contains."""

    def __init__(self, files: dict[str, str], build_ok=True, build_log="", running=True, logs=""):
        self.files, self.build_ok, self.build_log = files, build_ok, build_log
        self.running, self.logs, self.calls, self.restarted = running, logs, [], set()

    def available(self):
        return True

    def run(self, cmd, on_line=None, timeout=None, env=None):
        self.calls.append(cmd)
        out, code = "", 0
        if cmd[:2] == ["git", "clone"]:
            dest = Path(cmd[-1])
            for name, content in self.files.items():
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                (dest / name).write_text(content)
            out = "Cloning into 'x'..."
        elif cmd[:1] == ["git"] and "rev-parse" in cmd:
            out = "abc1234def5678"
        elif cmd[:1] == ["git"] and "log" in cmd:
            out = "feat: hello"
        elif cmd[:2] == ["docker", "build"]:
            out = self.build_log or "#1 DONE"
            code = 0 if self.build_ok else 1
        elif cmd[:2] == ["docker", "inspect"]:
            up = self.running or cmd[-1] in self.restarted
            out = json.dumps({"Running": up, "ExitCode": 0 if up else 1, "OOMKilled": False})
        elif cmd[:2] == ["docker", "logs"]:
            out = self.logs
        elif cmd[:2] == ["docker", "stats"]:
            out = json.dumps({"CPUPerc": "1.5%", "MemUsage": "42.1MiB / 512MiB"})
        elif cmd[:2] == ["docker", "restart"]:
            self.restarted.add(cmd[-1])
            out = cmd[-1]
        for ln in out.splitlines():
            if on_line:
                on_line(ln)
        return code, out


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "apps_workdir", str(tmp_path / "work"))
    monkeypatch.setattr(get_settings(), "apps_domain", "devnexa.space")
    monkeypatch.setattr(get_settings(), "apps_health_timeout", 0)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    monkeypatch.setattr(github, "get_token", lambda db: "ghp_secret_token_123")
    monkeypatch.setattr(deploy, "probe", lambda url: (200, 3.0))

    def use(**kw):
        ex = FakeExecutor(**kw)
        monkeypatch.setattr(deploy, "executor", ex)
        return ex
    return use


def deploy_repo(client, repo="vaibhav/shop-api", **kw):
    r = client.post("/api/apps/deploy", json={"repo": repo, "branch": "main", **kw})
    assert r.status_code == 201, r.text
    return client.get(f"/api/apps/deployments/{r.json()['id']}").json()


# ------------------------------------------------------------- build plans
def test_detect_python_fastapi(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    plan = buildplan.detect(tmp_path)
    assert plan.kind == "python-fastapi" and plan.port == 8000
    assert '"uvicorn", "main:app"' in plan.generated


def test_detect_dockerfile_and_vite(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM node\nEXPOSE 4000\n")
    assert buildplan.detect(tmp_path).port == 4000
    (tmp_path / "Dockerfile").unlink()
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite", "build": "vite build"},
                                                       "devDependencies": {"vite": "5"}}))
    plan = buildplan.detect(tmp_path)
    assert plan.kind == "node-static" and plan.port == 80 and "/app/dist" in plan.generated


def test_not_buildable(tmp_path):
    (tmp_path / "README.md").write_text("hi")
    with pytest.raises(buildplan.NotBuildable):
        buildplan.detect(tmp_path)


# ----------------------------------------------------------------- deploys
def test_deploy_success_gives_url_and_hides_token(client, fake):
    ex = fake(files={"requirements.txt": "flask\n", "app.py": "from flask import Flask\napp = Flask(__name__)\n"})
    d = deploy_repo(client)
    assert d["status"] == "running" and d["health"] == "healthy"
    assert d["url"] == "https://shop-api.devnexa.space"
    assert d["build_kind"] == "python-flask"
    assert "ghp_secret_token_123" not in d["build_log"]
    run = next(c for c in ex.calls if c[:2] == ["docker", "run"])
    assert "--network" in run and "PORT=8000" in run

    # Caddy asks the backend where to send traffic for the app's hostname
    r = client.get("/api/apps/route", headers={"X-Forwarded-Host": "shop-api.devnexa.space"})
    assert r.status_code == 200 and r.headers["x-devnexa-upstream"] == f"{d['container']}:8000"
    assert client.get("/api/apps/route", headers={"X-Forwarded-Host": "nope.devnexa.space"}).status_code == 404
    assert client.get("/api/apps/tls-check?domain=shop-api.devnexa.space").status_code == 200
    assert client.get("/api/apps/tls-check?domain=evil.example.com").status_code == 404

    # a second deploy replaces the first (blue/green) and the old container is removed
    d2 = deploy_repo(client)
    assert client.get(f"/api/apps/deployments/{d['id']}").json()["status"] == "replaced"
    assert ["docker", "rm", "-f", d["container"]] in ex.calls
    apps = {a["app"]: a for a in client.get("/api/apps").json()}
    assert apps["shop-api"]["live"]["id"] == d2["id"]


def test_lockfile_failure_is_fixed_automatically(client, fake):
    pkg = json.dumps({"scripts": {"start": "node server.js"}})
    ex = fake(files={"package.json": pkg, "package-lock.json": "{}", "server.js": ""}, build_ok=False,
              build_log="npm ERR! code EUSAGE\nnpm ERR! `npm ci` can only install packages when your package.json "
                        "and package-lock.json are in sync")
    calls = []

    def builds_after_fix(cmd, on_line=None, timeout=None, env=None):
        if cmd[:2] == ["docker", "build"]:
            calls.append(Path(cmd[cmd.index("-f") + 1]).read_text())
            ex.build_ok = len(calls) > 1
        return FakeExecutor.run(ex, cmd, on_line, timeout, env)
    ex.run = builds_after_fix
    d = deploy_repo(client, repo="vaibhav/node-shop")
    assert d["status"] == "failed"
    assert d["diagnosis"]["category"] == "npm_lockfile"
    rem = d["remediations"][0]
    assert rem["action"] == "use_npm_install" and rem["decision"] == "auto_execute"
    assert rem["status"] == "verified"
    assert "npm ci" in calls[0] and "RUN npm install" in calls[1]
    child = client.get("/api/apps/deployments?app=node-shop").json()[0]
    assert child["status"] == "running" and child["parent_id"] == d["id"]
    # the fix is remembered and recalled for the next similar failure
    assert any(m["action"] == "use_npm_install" for m in app_doctor.recall(
        __import__("app.db", fromlist=["SessionLocal"]).SessionLocal(), app_doctor.symptoms(d["diagnosis"])))


def test_missing_python_module_proposes_fix_pr_needing_approval(client, fake, monkeypatch):
    fake(files={"requirements.txt": "flask\n", "app.py": "import yaml\nfrom flask import Flask\napp = Flask(__name__)\n"},
         running=False, logs="Traceback (most recent call last):\nModuleNotFoundError: No module named 'yaml'")
    opened = {}
    monkeypatch.setattr(github, "open_fix_pr", lambda token, repo, base, sha, files, title, body, branch:
                        opened.update(files=files, branch=branch) or "https://github.com/vaibhav/flasky/pull/7")
    d = deploy_repo(client, repo="vaibhav/flasky")
    assert d["status"] == "failed"
    diag = d["diagnosis"]
    assert diag["category"] == "python_module_missing" and diag["action"] == "open_fix_pr"
    assert diag["patch"]["requirements.txt"].endswith("pyyaml\n")
    rem = d["remediations"][0]
    assert rem["risk"] == "high" and rem["status"] == "pending_approval"
    r = client.post(f"/api/apps/deployments/{d['id']}/remediations/0/approve", json={"actor": "vaibhav"}).json()
    assert r["pr_url"].endswith("/pull/7")
    assert opened["files"] == diag["patch"]
    assert client.post(f"/api/apps/deployments/{d['id']}/remediations/0/approve", json={}).status_code == 409


def test_runtime_crash_is_restarted_and_verified(client, fake):
    ex = fake(files={"index.html": "<h1>hi</h1>"})
    d = deploy_repo(client, repo="vaibhav/site")
    assert d["status"] == "running"
    ex.running, ex.logs = False, "Error: something broke\n    at main (/app/server.js:3)"
    deploy.monitor_tick()
    d = client.get(f"/api/apps/deployments/{d['id']}").json()
    rem = d["remediations"][-1]
    assert rem["action"] == "restart_app" and rem["decision"] == "auto_execute" and rem["status"] == "executed"
    assert d["health"] == "recovering"
    ex.logs = "GET / 200"
    deploy.monitor_tick()
    d = client.get(f"/api/apps/deployments/{d['id']}").json()
    assert d["health"] == "healthy" and d["remediations"][-1]["status"] == "verified"
    assert d["metrics"]["last"]["mem_mb"] == 42.1


def test_build_log_stream_replays_stored_log(client, fake):
    fake(files={"index.html": "x"})
    d = deploy_repo(client, repo="vaibhav/static-two")
    body = client.get(f"/api/apps/deployments/{d['id']}/logs/stream").text
    assert "event: line" in body and "Deployed" in body and "event: end" in body


def test_admin_key_required_when_set(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "s3cret")
    assert client.get("/api/apps").status_code == 401
    assert client.get("/api/apps", headers={"X-DevNexa-Key": "s3cret"}).status_code == 200
    assert client.get("/api/apps/config").json()["admin_required"] is True
