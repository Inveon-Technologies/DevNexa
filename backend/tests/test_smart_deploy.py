"""Smart multi-language deploys, language-aware fixes, merge conflict resolution, auto-deploy and zero downtime."""

import json
import subprocess
from pathlib import Path

import pytest

from app.agents import deploy, fixer
from app.config import get_settings
from app.db import session_scope
from app.models import AppDeployment, AppPreview
from app.services import app_doctor, buildplan, github, merger

from test_deploy import FakeExecutor, deploy_repo, fake  # noqa: F401  (fixture)
from test_live import FILES, _fake_github, deploy_app


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    return root


# ------------------------------------------------------------- detection
POM = """<project><parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
</parent><properties><java.version>17</java.version></properties></project>"""


def test_java_maven_spring_boot(tmp_path):
    write(tmp_path, {"pom.xml": POM, "src/main/resources/application.properties": "server.port=9090\n"})
    plan = buildplan.detect(tmp_path)
    assert plan.kind == "java-spring" and plan.port == 9090
    assert plan.stack == "Java 17 · Spring Boot · Maven"
    assert "maven:3.9-eclipse-temurin-17" in plan.generated and "eclipse-temurin:17-jre" in plan.generated
    assert "SERVER_PORT=9090" in plan.generated


def test_gradle_kotlin_with_wrapper(tmp_path):
    write(tmp_path, {"build.gradle.kts": 'plugins { kotlin("jvm"); id("org.springframework.boot") }\n'
                                         "kotlin { jvmToolchain(21) }\njava { toolchain { languageVersion = "
                                         "JavaLanguageVersion.of(21) } }\n", "gradlew": "#!/bin/sh\n"})
    plan = buildplan.detect(tmp_path)
    assert plan.language == "Kotlin" and plan.tool == "Gradle" and plan.version == "21"
    assert "./gradlew --no-daemon -q bootJar -x test" in plan.generated


def test_java_war_runs_on_tomcat(tmp_path):
    write(tmp_path, {"pom.xml": "<project><packaging>war</packaging><properties><maven.compiler.source>11"
                                "</maven.compiler.source></properties></project>"})
    plan = buildplan.detect(tmp_path)
    assert plan.kind == "java-war" and "tomcat:10.1-jre11" in plan.generated


@pytest.mark.parametrize("files,kind,stack,needle", [
    ({"go.mod": "module x\n\ngo 1.22\nrequire github.com/gin-gonic/gin v1\n", "cmd/api/main.go": "package main"},
     "go", "Go 1.22 · Gin · go modules", "go build -ldflags=\"-s -w\" -o /out/app ./cmd/api"),
    ({"Cargo.toml": '[package]\nname = "shop"\n\n[dependencies]\naxum = "0.7"\n'}, "rust", "Rust · Axum · Cargo",
     "cargo build --release --bin shop"),
    ({"Gemfile": "source 'https://rubygems.org'\ngem 'rails', '~> 7.1'\n", ".ruby-version": "3.2.2\n"},
     "ruby-rails", "Ruby 3.2 · Rails · Bundler", "bin/rails server -b 0.0.0.0 -p 3000"),
    ({"composer.json": '{"require": {"php": "^8.2"}}', "artisan": "", "public/index.php": "<?php"},
     "php-laravel", "PHP 8.2 · Laravel · Composer", "APACHE_DOCUMENT_ROOT=/var/www/html/public"),
    ({"Shop.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net9.0'
                     "</TargetFramework></PropertyGroup></Project>"}, "dotnet", "C# 9.0 · ASP.NET Core · dotnet",
     'ENTRYPOINT ["dotnet", "Shop.dll"]'),
    ({"deno.json": '{"tasks": {"start": "deno run -A main.ts"}}', "main.ts": ""}, "deno", "TypeScript 2 · Deno",
     '["deno", "task", "start"]'),
    ({"package.json": '{"scripts": {"start": "bun run index.ts"}, "dependencies": {"hono": "4"}}', "bun.lockb": "x"},
     "bun", "TypeScript · Hono · Bun", "FROM oven/bun:1"),
])
def test_other_languages(tmp_path, files, kind, stack, needle):
    plan = buildplan.detect(write(tmp_path, files))
    assert plan.kind == kind and plan.stack == stack
    assert needle in plan.generated


def test_python_poetry_version_and_django_gunicorn(tmp_path):
    write(tmp_path, {"pyproject.toml": "[tool.poetry]\nname='x'\n[tool.poetry.dependencies]\ndjango='5'\n",
                     "poetry.lock": "", ".python-version": "3.11.9\n",
                     "manage.py": "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'shop.settings')",
                     "shop/wsgi.py": ""})
    plan = buildplan.detect(tmp_path)
    assert plan.stack == "Python 3.11 · Django · Poetry"
    assert "FROM python:3.11-slim" in plan.generated and "poetry install" in plan.generated
    assert '"shop.wsgi:application"' in plan.generated and "pip install --no-cache-dir gunicorn" in plan.generated


def test_requires_python_upper_bound(tmp_path):
    write(tmp_path, {"pyproject.toml": '[project]\nrequires-python = ">=3.9,<3.12"\n', "main.py": ""})
    assert buildplan.detect(tmp_path).version == "3.11"


def test_node_framework_and_hardcoded_port(tmp_path):
    write(tmp_path, {"package.json": json.dumps({"main": "server.js", "dependencies": {"express": "4"}}),
                     "server.js": "app.listen(5000)"})
    plan = buildplan.detect(tmp_path)
    assert plan.stack == "Node 20 · Express · npm" and plan.port == 5000


def test_monorepo_subfolder_is_found(tmp_path):
    write(tmp_path, {"README.md": "x", "backend/requirements.txt": "flask\n",
                     "backend/app.py": "from flask import Flask\napp = Flask(__name__)\n", "docs/index.html": ""})
    plan = buildplan.detect(tmp_path)
    assert plan.subdir == "backend" and plan.kind == "python-flask"
    write(tmp_path, {"worker/go.mod": "module w\ngo 1.23\n", "worker/main.go": ""})
    plan = buildplan.detect(tmp_path)  # two candidates: the preferred name wins
    assert plan.subdir == "backend" and "worker" in plan.notes[0]
    write(tmp_path, {"other/go.mod": "module o\n"})
    (tmp_path / "backend/requirements.txt").unlink()
    (tmp_path / "backend/app.py").unlink()
    with pytest.raises(buildplan.NotBuildable, match="several folders"):
        buildplan.detect(tmp_path)


def test_mixed_repo_mentions_the_other_stack(tmp_path):
    write(tmp_path, {"package.json": json.dumps({"scripts": {"start": "node index.js"}}), "index.js": "",
                     "requirements.txt": "requests\n"})
    plan = buildplan.detect(tmp_path)
    assert plan.language == "Node" and any("python" in n for n in plan.notes)


def test_detect_api_clones_and_reports_the_stack(client, fake):
    fake(files={"pom.xml": POM})
    r = client.post("/api/apps/detect", json={"repo": "vaibhav/shop", "branch": "main"})
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["stack"] == "Java 17 · Spring Boot · Maven" and r.json()["languages"] == ["java"]


def test_deploy_into_subfolder_remembers_it(client, fake):
    fake(files={"api/requirements.txt": "fastapi\n", "api/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    d = deploy_repo(client, repo="vaibhav/mono", app="mono")
    assert d["status"] == "running" and d["root_dir"] == "api"
    assert d["metrics"]["stack"]["stack"] == "Python 3.12 · FastAPI · pip"
    assert "[detect] stack: Python 3.12 · FastAPI · pip" in d["build_log"]


# -------------------------------------------------- language-aware fixes
@pytest.mark.parametrize("log,version", [
    ("[ERROR] Failed to execute goal ... Fatal error compiling: error: release version 21 not supported", "21"),
    ("java.lang.UnsupportedClassVersionError: App has been compiled by a more recent version of the Java Runtime "
     "(class file version 65.0)", "21"),
    ('error shop@1.0.0: The engine "node" is incompatible with this module. Expected version ">=22.0.0".', "22"),
    ("ERROR: Package 'shop' requires a different Python: 3.12.3 not in '>=3.13'", "3.13"),
    ("go: go.mod requires go >= 1.24 (running go 1.23.4)", "1.24"),
])
def test_runtime_version_mismatch_is_understood(log, version):
    d = app_doctor.heuristic("build", log)
    assert d["action"] == "change_runtime" and d["runtime_version"] == version


def test_compile_and_dependency_errors_by_language():
    assert app_doctor.heuristic("build", "[ERROR] COMPILATION ERROR :\n[ERROR] /src/App.java:[3,1] error: cannot find symbol")["category"] == "syntax_error"
    assert app_doctor.heuristic("build", "error[E0425]: cannot find value `x` in this scope")["category"] == "syntax_error"
    assert app_doctor.heuristic("build", "Your requirements could not be resolved to an installable set")["category"] == "deps_resolution"
    assert app_doctor.heuristic("start", "no main manifest attribute, in /app/app.jar")["category"] == "jar_no_main"


class FailOnceExecutor(FakeExecutor):
    """The first docker build fails with `first_log`; it records each Dockerfile it was asked to build."""

    def __init__(self, first_log: str, **kw):
        super().__init__(**kw)
        self.first_log, self.builds = first_log, []

    def run(self, cmd, on_line=None, timeout=None, env=None):
        if cmd[:2] == ["docker", "build"]:
            self.builds.append(Path(cmd[cmd.index("-f") + 1]).read_text())
            if len(self.builds) == 1:
                self.calls.append(cmd)
                if on_line:
                    on_line(self.first_log)
                return 1, self.first_log
        return super().run(cmd, on_line, timeout, env)


def test_java_version_mismatch_rebuilds_on_the_right_jdk(client, fake, monkeypatch):
    fake(files={})
    ex = FailOnceExecutor("[ERROR] error: release version 25 not supported", files={"pom.xml": POM})
    monkeypatch.setattr(deploy, "executor", ex)
    first = deploy_repo(client, repo="vaibhav/jdk", app="jdk")
    assert first["status"] == "failed"
    full = client.get(f"/api/apps/deployments/{first["id"]}").json()
    rem = full["remediations"][0]
    assert rem["action"] == "change_runtime" and rem["decision"] == "auto_execute"
    assert "temurin-17" in ex.builds[0] and "temurin-25" in ex.builds[1]
    with session_scope() as db:
        child = db.query(AppDeployment).filter(AppDeployment.parent_id == first["id"]).one()
        assert child.status == "running" and child.options["runtime_version"] == "25"


def test_repo_dockerfile_runtime_mismatch_is_explained_not_rebuilt():
    with session_scope() as db:
        d = app_doctor.diagnose(db, "build", "release version 21 not supported", build_kind="dockerfile")
    assert d["action"] == "escalate" and "FROM image" in d["suggestion"]


# ------------------------------------------------------ merge conflicts
def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare 'GitHub' repo with main and a conflicting PR branch."""
    monkeypatch.setattr(get_settings(), "apps_workdir", str(tmp_path / "work"))
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed.mkdir()
    git(seed, "init", "-b", "main")
    base = {"package.json": json.dumps({"name": "shop", "dependencies": {"express": "4"}}, indent=2) + "\n",
            "package-lock.json": '{"lockfileVersion": 3, "v": "base"}\n',
            "requirements.txt": "flask==3.0\n", ".gitignore": "node_modules\n",
            "app.py": "import os\n\n\ndef total(cart):\n    return sum(cart)\n"}
    write(seed, base)
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "base")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", "main")
    git(seed, "checkout", "-b", "feature")
    write(seed, {"package.json": json.dumps({"name": "shop", "dependencies": {"express": "4", "zod": "3"}}, indent=2) + "\n",
                 "package-lock.json": '{"lockfileVersion": 3, "v": "feature"}\n',
                 "requirements.txt": "flask==3.0\nrequests==2.32\n", ".gitignore": "node_modules\n.env\n",
                 "app.py": "import os\nimport json\n\n\ndef total(cart):\n    return sum(cart)\n"})
    git(seed, "commit", "-am", "feature")
    git(seed, "push", "origin", "feature")
    git(seed, "checkout", "main")
    write(seed, {"package.json": json.dumps({"name": "shop", "dependencies": {"express": "4", "cors": "2"}}, indent=2) + "\n",
                 "package-lock.json": '{"lockfileVersion": 3, "v": "main"}\n',
                 "requirements.txt": "flask==3.0\ngunicorn==22\n", ".gitignore": "node_modules\ndist\n",
                 "app.py": "import os\nimport re\n\n\ndef total(cart):\n    return sum(cart)\n"})
    git(seed, "commit", "-am", "main moves on")
    git(seed, "push", "origin", "main")
    monkeypatch.setattr(merger, "remote_url", lambda token, repo: str(bare))
    return seed


def show(seed, path):
    git(seed, "fetch", "origin")
    return subprocess.run(["git", "show", f"origin/feature:{path}"], cwd=seed, capture_output=True, text=True).stdout


def test_merge_agent_resolves_common_conflicts(remote):
    res = merger.update_branch("tok", "vaibhav/shop", "feature", "main")
    assert res.status == "resolved"
    strategies = {f["path"]: f["strategy"] for f in res.files}
    assert strategies == {"package.json": "JSON three-way merge", "package-lock.json": "lockfile from base",
                          "requirements.txt": "union of lines", ".gitignore": "union of lines", "app.py": "hunk rules"}
    assert json.loads(show(remote, "package.json"))["dependencies"] == {"express": "4", "zod": "3", "cors": "2"}
    assert "main" in show(remote, "package-lock.json")
    assert show(remote, "requirements.txt").split() == ["flask==3.0", "requests==2.32", "gunicorn==22"]
    assert show(remote, "app.py").startswith("import os\nimport json\nimport re\n")
    assert "<<<<<<<" not in show(remote, "app.py")
    # running it again: nothing left to do
    assert merger.update_branch("tok", "vaibhav/shop", "feature", "main").status == "up_to_date"


def test_merge_agent_stops_on_a_real_code_conflict(remote):
    git(remote, "checkout", "feature")
    write(remote, {"app.py": "import os\nimport json\n\n\ndef total(cart):\n    return sum(c.price for c in cart)\n"})
    git(remote, "commit", "-am", "price")
    git(remote, "push", "origin", "feature")
    git(remote, "checkout", "main")
    write(remote, {"app.py": "import os\nimport re\n\n\ndef total(cart):\n    return round(sum(cart), 2)\n"})
    git(remote, "commit", "-am", "round")
    git(remote, "push", "origin", "main")
    before = show(remote, "app.py")
    with pytest.raises(merger.MergeError) as e:
        merger.update_branch("tok", "vaibhav/shop", "feature", "main")
    assert e.value.unresolved == ["app.py"] and "DEVNEXA_LLM_PROVIDER" in str(e.value)
    assert show(remote, "app.py") == before  # nothing was pushed


def test_hunk_rules():
    assert merger.resolve_hunk("a\n", "a\n", "b\n") == ("b\n", "only the base branch changed it")
    assert merger.resolve_hunk("x = 1\n", "", "x  =  1\n")[0] == "x = 1\n"
    assert merger.resolve_hunk("a\nb\nc\n", "z\n", "a\nc\n")[0] == "a\nb\nc\n"
    assert merger.resolve_hunk("a = 1\n", "a = 0\n", "a = 2\n")[0] is None
    with pytest.raises(ValueError, match="dependencies.react"):
        merger.merge_json({"dependencies": {"react": "17"}}, {"dependencies": {"react": "18"}},
                          {"dependencies": {"react": "19"}})


def test_conflicting_fix_is_resolved_retested_and_merged(client, fake, monkeypatch):
    fake(files=FILES)
    calls = []
    _fake_github(monkeypatch, calls)
    state = {"mergeable": False}
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: {"merged": False, "mergeable": state["mergeable"],
                                                             "mergeable_state": "dirty" if not state["mergeable"] else "clean",
                                                             "head_sha": "h"})
    monkeypatch.setattr(github, "comment_pr", lambda token, repo, n, body: calls.append(("comment", n, body)))

    def fake_update(token, repo, head, base, push=True):
        state["mergeable"] = True
        return merger.MergeResult("resolved", head, base, "m3rg3", [
            {"path": "package.json", "strategy": "JSON three-way merge", "detail": "kept both sides' keys"}])
    monkeypatch.setattr(merger, "update_branch", fake_update)
    deploy_app(client, "cf-app")
    with session_scope() as db:
        pv_id = fixer.preview_branch(db, "cf-app", "feature/cart", "vaibhav")
    res = client.post(f"/api/live/previews/{pv_id}/approve", json={"actor": "vaibhav"})
    assert res.status_code == 200 and res.json()["status"] == "resolving_conflicts", res.text
    # resolved -> pushed -> test URL rebuilt -> healthy -> merged -> app rolled out (runner is synchronous in tests)
    assert ("merge", 7) in calls
    assert any(c[0] == "comment" and "package.json" in c[2] for c in calls)
    with session_scope() as db:
        pv = db.get(AppPreview, pv_id)
        assert pv.status == "merged"
        retest = db.query(AppDeployment).filter(AppDeployment.app == pv.preview_app).order_by(AppDeployment.id.desc()).first()
        assert retest.options["merge_after"]["preview_id"] == pv_id and retest.triggered_by == "merge-agent"
        live = db.query(AppDeployment).filter(AppDeployment.app == "cf-app", AppDeployment.status == "running").one()
        assert live.options["source"] == "merge"


def test_unresolvable_conflict_is_left_for_a_developer(client, fake, monkeypatch):
    fake(files=FILES)
    calls = []
    _fake_github(monkeypatch, calls)
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: {"merged": False, "mergeable": False,
                                                             "mergeable_state": "dirty", "head_sha": "h"})
    monkeypatch.setattr(github, "comment_pr", lambda token, repo, n, body: calls.append(("comment", n, body)))

    def boom(*a, **k):
        raise merger.MergeError("1 file(s) need a developer: app.py", ["app.py"])
    monkeypatch.setattr(merger, "update_branch", boom)
    deploy_app(client, "cf2-app")
    with session_scope() as db:
        pv_id = fixer.preview_branch(db, "cf2-app", "feature/x", "vaibhav")
    client.post(f"/api/live/previews/{pv_id}/approve", json={"actor": "vaibhav"})
    pv = next(p for p in client.get("/api/live/apps/cf2-app/previews").json() if p["id"] == pv_id)
    assert pv["status"] == "conflict" and "app.py" in pv["note"]
    assert ("merge", 7) not in calls and any(c[0] == "comment" and "`app.py`" in c[2] for c in calls)


# ------------------------------------------------ auto-deploy, zero downtime
def test_new_commit_on_the_branch_is_rolled_out_once(client, fake, monkeypatch):
    fake(files=FILES)
    first = deploy_app(client, "auto-app")
    monkeypatch.setattr(github, "branch_sha", lambda token, repo, branch: "abc1234def5678")
    with session_scope() as db:  # same commit as deployed
        assert all(db.get(AppDeployment, i).app != "auto-app" for i in deploy.autodeploy_tick(force=True))
    monkeypatch.setattr(github, "branch_sha", lambda token, repo, branch: "fe11a5e0000")
    started = deploy.autodeploy_tick(force=True)
    with session_scope() as db:
        mine = [i for i in started if db.get(AppDeployment, i).app == "auto-app"]
        assert len(mine) == 1
        new = db.get(AppDeployment, mine[0])
        assert new.triggered_by == "auto-deploy" and new.status == "running" and new.options["source"] == "auto-deploy"
        assert db.get(AppDeployment, first["id"]).status == "replaced"
    with session_scope() as db:  # each commit is tried once
        assert all(db.get(AppDeployment, i).app != "auto-app" for i in deploy.autodeploy_tick(force=True))
    r = client.put("/api/live/apps/auto-app/settings", json={"steps": {"auto_deploy": False}})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(github, "branch_sha", lambda token, repo, branch: "0ther5ha")
    with session_scope() as db:
        assert all(db.get(AppDeployment, i).app != "auto-app" for i in deploy.autodeploy_tick(force=True))


def test_switch_waits_for_consecutive_health_checks_and_drains(client, fake, monkeypatch):
    ex = fake(files=FILES)
    seq = iter([(200, 1.0), (502, 1.0), (200, 1.0), (200, 1.0), (200, 1.0)] + [(200, 1.0)] * 20)
    probes = []
    monkeypatch.setattr(deploy, "probe", lambda url: probes.append(url) or next(seq))
    monkeypatch.setattr(get_settings(), "apps_health_timeout", 30)
    old = deploy_app(client, "zd-app")
    assert len(probes) == 5  # 200, 502 (counter resets), then three in a row
    timers = []
    monkeypatch.setattr(get_settings(), "apps_drain_seconds", 10)
    monkeypatch.setattr(deploy.threading, "Timer", lambda s, fn: timers.append((s, fn)) or type(
        "T", (), {"daemon": True, "start": lambda self: None})())
    n = len(ex.calls)
    new = deploy_app(client, "zd-app")
    assert new["status"] == "running"
    assert ["docker", "rm", "-f", old["container"]] not in ex.calls[n:]  # still draining
    assert "drains for 10s" in new["build_log"]
    assert timers and timers[0][0] == 10
    timers[0][1]()
    assert ["docker", "rm", "-f", old["container"]] in ex.calls[n:]
