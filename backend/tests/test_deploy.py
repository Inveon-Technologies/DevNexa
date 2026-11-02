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


pushed: list[dict] = []


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "apps_workdir", str(tmp_path / "work"))
    monkeypatch.setattr(get_settings(), "apps_domain", "devnexa.space")
    monkeypatch.setattr(get_settings(), "apps_health_timeout", 0)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    monkeypatch.setattr(github, "get_token", lambda db, login=None: "ghp_secret_token_123")
    monkeypatch.setattr(deploy, "probe", lambda url: (200, 3.0))
    monkeypatch.setattr(github, "branch_sha", lambda token, repo, branch: "base000sha")
    pushed.clear()
    monkeypatch.setattr(github, "push_branch", lambda token, repo, sha, files, branch, message="":
                        pushed.append({"repo": repo, "sha": sha, "files": files, "branch": branch}))
    monkeypatch.setattr(github, "open_pr", lambda token, repo, base, branch, title, body, draft=False:
                        pushed.append({"pr": branch, "base": base, "title": title, "body": body})
                        or f"https://github.com/{repo}/pull/{len(pushed)}")

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


def test_missing_python_module_is_fixed_on_a_branch_and_verified_before_the_pr(client, fake):
    files = {"requirements.txt": "flask\n", "app.py": "import yaml\nfrom flask import Flask\napp = Flask(__name__)\n"}
    ex = fake(files=files, running=False,
              logs="Traceback (most recent call last):\nModuleNotFoundError: No module named 'yaml'")
    real_run = ex.run

    def fixed_on_branch(cmd, on_line=None, timeout=None, env=None):
        if cmd[:2] == ["git", "clone"]:  # the fix branch has the patched requirements.txt and starts fine
            on_fix = "devnexa/fix-flasky-" in " ".join(cmd)
            ex.files = {**files, "requirements.txt": "flask\npyyaml\n"} if on_fix else files
            ex.running = on_fix
        return real_run(cmd, on_line, timeout, env)
    ex.run = fixed_on_branch
    d = client.get(f"/api/apps/deployments/{deploy_repo(client, repo='vaibhav/flasky')['id']}").json()
    assert d["status"] == "failed"
    diag = d["diagnosis"]
    assert diag["category"] == "python_module_missing" and diag["action"] == "open_fix_pr"
    assert diag["patch"]["requirements.txt"].endswith("pyyaml\n")
    rem = d["remediations"][0]
    assert rem["decision"] == "auto_execute" and rem["status"] == "verified"
    branch = f"devnexa/fix-flasky-{d['id']}"
    push = next(p for p in pushed if p.get("branch") == branch)
    assert push["files"] == diag["patch"] and push["sha"] == "abc1234def5678"
    # the PR is opened only after the preview of the fix branch is healthy, and links to it
    pr = next(p for p in pushed if p.get("pr") == branch)
    assert pr["base"] == "main" and f"https://flasky-fix-{d['id']}.devnexa.space" in pr["body"]
    assert rem["pr_url"].startswith("https://github.com/vaibhav/flasky/pull/")
    assert rem["preview_url"] == f"https://flasky-fix-{d['id']}.devnexa.space"
    preview = client.get(f"/api/apps/deployments/{rem['preview_id']}").json()
    assert preview["branch"] == branch and preview["status"] == "running" and preview["parent_id"] == d["id"]


def test_fix_branch_that_still_fails_gets_no_pr(client, fake):
    fake(files={"requirements.txt": "flask\n", "app.py": "import yaml\n"}, running=False,
         logs="ModuleNotFoundError: No module named 'yaml'")
    d = deploy_repo(client, repo="vaibhav/stubborn")
    d = client.get(f"/api/apps/deployments/{d['id']}").json()
    rem = d["remediations"][0]
    assert rem["status"] == "failed" and "No pull request" in rem["detail"]
    assert not any(p.get("pr") for p in pushed)


def test_fix_prs_wait_for_approval_when_auto_fix_is_off(client, fake):
    client.put("/api/policies/autonomy", json={"auto_fix_branch": False})
    try:
        fake(files={"requirements.txt": "flask\n", "app.py": "import yaml\n"}, running=False,
             logs="ModuleNotFoundError: No module named 'yaml'")
        d = deploy_repo(client, repo="vaibhav/careful")
        rem = d["remediations"][0]
        assert rem["action"] == "open_fix_pr" and rem["status"] == "pending_approval"
        assert not pushed
        client.post(f"/api/apps/deployments/{d['id']}/remediations/0/approve", json={"actor": "vaibhav"})
        assert any(p.get("branch") == f"devnexa/fix-careful-{d['id']}" for p in pushed)
        assert client.post(f"/api/apps/deployments/{d['id']}/remediations/0/approve", json={}).status_code == 409
    finally:
        client.put("/api/policies/autonomy", json={"auto_fix_branch": True})


def test_repo_without_dockerfile_gets_a_dockerfile_pr_once(client, fake):
    fake(files={"requirements.txt": "fastapi\n", "main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    d = deploy_repo(client, repo="vaibhav/no-docker")
    rem = next(r for r in d["remediations"] if r["action"] == "add_dockerfile")
    assert rem["status"] == "verified" and rem["pr_url"]
    push = next(p for p in pushed if p.get("branch") == "devnexa/dockerize-no-docker")
    assert '"uvicorn", "main:app"' in push["files"]["Dockerfile"] and ".dockerignore" in push["files"]
    pushed.clear()
    deploy_repo(client, repo="vaibhav/no-docker")
    assert not pushed  # already proposed for this repo and branch


def test_broken_repo_dockerfile_is_replaced_by_a_generated_one(client, fake):
    ex = fake(files={"Dockerfile": "FROM node:20\nRUN make everything\n", "package.json": json.dumps(
        {"scripts": {"start": "node server.js"}}), "server.js": ""}, build_ok=False,
        build_log="#5 [2/2] RUN make everything\n#5 0.2 /bin/sh: make: not found\n#5 ERROR: process did not complete")
    real_run = ex.run

    def generated_builds(cmd, on_line=None, timeout=None, env=None):
        if cmd[:2] == ["git", "clone"]:
            on_fix = "devnexa/fix-" in " ".join(cmd)
            ex.build_ok = on_fix
            if on_fix:
                ex.files = {**ex.files, "Dockerfile": next(p for p in pushed if "files" in p)["files"]["Dockerfile"]}
        return real_run(cmd, on_line, timeout, env)
    ex.run = generated_builds
    d = client.get(f"/api/apps/deployments/{deploy_repo(client, repo='vaibhav/bad-docker')['id']}").json()
    assert d["diagnosis"]["category"] == "dockerfile_broken"
    assert "npm" in d["diagnosis"]["patch"]["Dockerfile"]
    assert d["remediations"][0]["pr_url"]


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


def test_pnpm_allows_dependency_build_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "next build", "start": "next start"},
                                                       "dependencies": {"next": "16"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    plan = buildplan.detect(tmp_path)
    assert "pnpm install --frozen-lockfile --config.dangerously-allow-all-builds=true" in plan.generated
    relaxed = buildplan.detect(tmp_path, {"npm_install": True})
    assert "--no-frozen-lockfile" in relaxed.generated
    diag = app_doctor.heuristic("build", "#8 7.154 Error: ERR_PNPM_IGNORED_BUILDS\n  Ignored build scripts: sharp@0.34.5")
    assert diag["category"] == "pnpm_ignored_builds" and diag["action"] == "retry_deploy"
