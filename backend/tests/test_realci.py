import json
from pathlib import Path

import pytest
from test_deploy import deploy_repo, fake  # noqa: F401  (fixture)

from app.agents import realci
from app.agents.cicd import create_run, run_pipeline
from app.agents.context import RealClock, RunContext
from app.config import get_settings
from app.db import SessionLocal
from app.models import PipelineRun
from app.services import github
from app.services.environment import SimulatedEnvironment

PYTEST_OK = "....\n4 passed in 0.52s"
PYTEST_FAIL = "F...\nFAILED tests/test_sample_app.py::test_orders - assert 500 == 200\n1 failed, 3 passed in 0.61s"


class LiveEnv(SimulatedEnvironment):
    mode = "live"


class FakeDocker:
    """git + docker for the live pipeline: records commands, answers like the real CLIs."""

    def __init__(self, build_ok=True, pytest_out=PYTEST_OK):
        self.build_ok, self.pytest_out = build_ok, pytest_out
        self.calls, self.image = [], "devnexa-sample-app"

    def available(self):
        return True

    def run(self, cmd, on_line=None, timeout=None, env=None):
        self.calls.append(cmd)
        code, out = 0, ""
        if cmd[:1] == ["git"] and "checkout" in cmd:
            work = Path(cmd[2])
            (work / "sample-app" / "tests").mkdir(parents=True)
            (work / "sample-app" / "Dockerfile").write_text("FROM python:3.12-slim\n")
        elif cmd[:2] == ["docker", "build"]:
            code, out = (0, "#9 DONE") if self.build_ok else (1, "ERROR: failed to solve: pip install")
        elif cmd[:2] == ["docker", "start"]:
            out = self.pytest_out
            code = 0 if "failed" not in out else 1
        elif cmd[:2] == ["docker", "inspect"] and "-f" in cmd:
            out = self.image
        elif cmd[:2] == ["docker", "inspect"]:
            out = json.dumps([{
                "Id": "0123456789abcdef", "Name": "/devnexa-sample-app",
                "Config": {"Image": self.image, "Env": ["PATH=/usr/bin", "LOADGEN=true"],
                           "Labels": {"com.docker.compose.service": "sample-app"}},
                "HostConfig": {"PortBindings": {"8000/tcp": [{"HostIp": "", "HostPort": "8081"}]},
                               "RestartPolicy": {"Name": "no"}},
                "Mounts": [{"Type": "volume", "Name": "devnexa_sampledata", "Destination": "/data", "RW": True}],
                "NetworkSettings": {"Networks": {"devnexa_default": {"Aliases": ["sample-app", "0123456789ab"]}}},
            }])
        elif cmd[:2] == ["docker", "run"]:
            self.image = cmd[-1]
        for ln in out.splitlines():
            if on_line:
                on_line(ln)
        return code, out


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "apps_workdir", str(tmp_path / "work"))
    monkeypatch.setattr(get_settings(), "github_repo", "Inveon-Technologies/DevNexa")
    monkeypatch.setattr(github, "get_token", lambda db: "ghp_token")
    monkeypatch.setattr(realci, "wait_ready", lambda timeout=None: True)

    def use(**kw):
        ex = FakeDocker(**kw)
        monkeypatch.setattr(realci, "executor", ex)
        ctx = RunContext(session_factory=SessionLocal, env=LiveEnv(), clock=RealClock(0))
        with SessionLocal() as db:
            run = create_run(db, commit_sha="a" * 40, message="feat: real ci", files=["app/main.py"],
                             trigger="github-push", repo="Inveon-Technologies/DevNexa")
            db.commit()
            run_id = run.id
        run_pipeline(ctx, run_id)
        with SessionLocal() as db:
            run = db.get(PipelineRun, run_id)
            return ex, run, {s.name: s for s in run.stages}
    return use


def test_parse_pytest():
    s = realci.parse_pytest(PYTEST_FAIL)
    assert (s["passed"], s["failed"], s["duration_s"]) == (3, 1, 0.61)
    assert s["failed_tests"] == ["tests/test_sample_app.py::test_orders"]


def test_live_pipeline_builds_tests_and_swaps_container(live):
    ex, run, st = live()
    assert run.status == "success"
    assert ["docker", "build", "--progress=plain", "-t", "devnexa/sample-app:aaaaaaa"] == ex.calls[
        next(i for i, c in enumerate(ex.calls) if c[:2] == ["docker", "build"])][:5]
    assert any(c[:2] == ["git", "-C"] and "fetch" in c for c in ex.calls)
    assert st["test"].details["real"] and st["test"].details["selected"] == 4
    run_cmd = next(c for c in ex.calls if c[:2] == ["docker", "run"])
    assert run_cmd[-1] == "devnexa/sample-app:aaaaaaa"
    assert "--network-alias" in run_cmd and "sample-app" in run_cmd and "devnexa_sampledata:/data" in run_cmd
    assert "0123456789ab" not in run_cmd and "PATH=/usr/bin" not in run_cmd


def test_live_pipeline_real_test_failure_gets_ai_feedback(live):
    ex, run, st = live(pytest_out=PYTEST_FAIL)
    assert run.status == "failed" and st["test"].status == "failed"
    assert st["deploy"].status == "skipped"
    assert "test_orders" in run.ai_feedback
    assert not any(c[:2] == ["docker", "run"] for c in ex.calls)


def test_live_pipeline_build_failure(live):
    _, run, st = live(build_ok=False)
    assert run.status == "failed" and st["build"].status == "failed" and st["test"].status == "skipped"


def test_live_pipeline_rolls_back_when_new_container_fails(live, monkeypatch):
    calls = iter([False, True])
    monkeypatch.setattr(realci, "wait_ready", lambda timeout=None: next(calls))
    ex, run, st = live()
    assert run.status == "failed" and st["deploy"].status == "failed"
    images = [c[-1] for c in ex.calls if c[:2] == ["docker", "run"]]
    assert images == ["devnexa/sample-app:aaaaaaa", "devnexa-sample-app"]


def test_fault_lab_runs_stay_simulated(monkeypatch):
    monkeypatch.setattr(realci, "executor", FakeDocker())
    monkeypatch.setattr(get_settings(), "github_repo", "Inveon-Technologies/DevNexa")
    assert realci.enabled(RunContext(SessionLocal, LiveEnv(), RealClock(0)), None)
    assert not realci.enabled(RunContext(SessionLocal, LiveEnv(), RealClock(0)), "memory_leak")
    assert not realci.enabled(RunContext(SessionLocal, SimulatedEnvironment(), RealClock(0)), None)


def test_actions_final_release_completes_run(client):
    base = {"run_key": "777-1", "commit_sha": "c" * 40, "message": "ci", "files": []}
    for stage in ("build", "test"):
        client.post("/api/webhooks/github/actions", json={**base, "stage": stage, "status": "success"})
    r = client.post("/api/webhooks/github/actions",
                    json={**base, "stage": "release", "status": "success", "final": True}).json()
    run = client.get(f"/api/pipelines/{r['run_id']}").json()
    assert run["status"] == "success"
    assert {s["name"]: s["status"] for s in run["stages"]}["deploy"] == "skipped"


def test_push_redeploys_apps_from_that_repo(client, fake):  # noqa: F811
    fake(files={"package.json": json.dumps({"scripts": {"start": "node index.js"}}), "index.js": ""})
    first = deploy_repo(client, repo="vaibhav/cd-app")
    assert first["status"] == "running"
    payload = {"ref": "refs/heads/main", "after": "d" * 40, "repository": {"full_name": "vaibhav/cd-app"},
               "head_commit": {"id": "d" * 40, "message": "feat: new page"}, "commits": []}
    r = client.post("/api/webhooks/github", json=payload, headers={"X-GitHub-Event": "push"}).json()
    assert len(r["redeploys"]) == 1 and "run_id" not in r
    new = client.get(f"/api/apps/deployments/{r['redeploys'][0]}").json()
    assert new["status"] == "running" and new["triggered_by"] == "github-push"
    assert client.get(f"/api/apps/deployments/{first['id']}").json()["status"] == "replaced"
