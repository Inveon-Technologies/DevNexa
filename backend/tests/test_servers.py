"""Remote servers: SSH keys, checks, remote commands, and deploying apps to another machine's Docker engine."""

import json
import os

import pytest
from test_deploy import FakeExecutor, deploy_repo, fake  # noqa: F401  (fixture re-export)

from app.agents import deploy
from app.config import get_settings
from app.db import SessionLocal
from app.services import servers

PROBE_OK = "\n".join([
    "__devnexa__=1", "os=Ubuntu 24.04.1 LTS", "kernel=6.8.0", "arch=x86_64", "hostname=worker-1", "user=ubuntu",
    "cpus=4", "mem_total_mb=7940", "mem_used_mb=1200", "disk_total=78G", "disk_free=60G", "disk_used_pct=23%",
    "load=0.10 0.05 0.01", "uptime=up 3 days", "docker=27.3.1", "docker_error=", "__containers__",
    json.dumps({"ID": "abc", "Names": "devnexa-app-shop-1", "Image": "devnexa-app/shop:1", "State": "running",
                "Status": "Up 2 hours", "Ports": "0.0.0.0:20001->8000/tcp", "Labels": "devnexa.app=shop"}),
    json.dumps({"ID": "def", "Names": "postgres", "Image": "postgres:16", "State": "exited", "Status": "Exited (0)",
                "Ports": "", "Labels": ""}),
])


class RemoteFake(FakeExecutor):
    """git + docker + ssh. Records which server each docker command was aimed at."""

    def __init__(self, ssh_out=PROBE_OK, ssh_code=0, files=None, **kw):
        super().__init__(files or {}, **kw)
        self.ssh_out, self.ssh_code, self.targets = ssh_out, ssh_code, []

    def run(self, cmd, on_line=None, timeout=None, env=None):
        if cmd[:1] == ["ssh"]:
            self.calls.append(cmd)
            for ln in self.ssh_out.splitlines():
                if on_line:
                    on_line(ln)
            return self.ssh_code, self.ssh_out
        if cmd[:1] == ["docker"]:
            t = servers.current()
            self.targets.append((cmd[1], t["name"] if t else None))
        return super().run(cmd, on_line, timeout, env)


@pytest.fixture
def ssh_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "servers_dir", str(tmp_path / "srv"))
    monkeypatch.setattr(get_settings(), "servers_ssh_config", str(tmp_path / "home" / ".ssh" / "config"))
    return tmp_path


def add(client, name="worker-1", **kw):
    r = client.post("/api/servers", json={"name": name, "host": "203.0.113.10", "username": "ubuntu", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_generated_and_pasted_keys():
    priv, pub = servers.generate_keypair("devnexa-x")
    assert "OPENSSH PRIVATE KEY" in priv and pub.startswith("ssh-ed25519 ") and pub.endswith(" devnexa-x")
    again, pub2 = servers.parse_private_key(priv, "devnexa-x")
    assert pub2 == pub
    with pytest.raises(servers.ServerError):
        servers.parse_private_key("not a key")


def test_add_server_never_returns_the_private_key(client, ssh_dirs, monkeypatch):
    monkeypatch.setattr(deploy, "executor", RemoteFake())
    s = add(client, "keys-test")
    assert s["status"] == "new" and s["public_key"].startswith("ssh-ed25519 ")
    assert "authorized_keys" in s["authorize_command"] and s["public_key"] in s["authorize_command"]
    assert "private_key" not in s and "PRIVATE KEY" not in json.dumps(client.get("/api/servers").json())
    assert client.post("/api/servers", json={"name": "keys-test", "host": "x.example.com"}).status_code == 409
    assert client.post("/api/servers", json={"name": "Bad Name!", "host": "x.example.com"}).status_code == 400
    assert client.post("/api/servers", json={"name": "evil", "host": "-oProxyCommand=x"}).status_code == 400


def test_check_reads_the_machine_and_writes_ssh_config(client, ssh_dirs, monkeypatch):
    ex = RemoteFake()
    monkeypatch.setattr(deploy, "executor", ex)
    s = add(client, "check-test")
    r = client.post(f"/api/servers/{s['id']}/check").json()
    assert r["status"] == "online" and r["facts"]["docker"] == "27.3.1" and r["facts"]["cpus"] == 4
    assert [c["name"] for c in r["containers"]] == ["devnexa-app-shop-1", "postgres"]
    assert r["containers"][0]["devnexa"] and r["facts"]["containers_running"] == 1
    ssh = ex.calls[-1]
    assert ssh[:2] == ["ssh", "-F"] and ssh[3] == f"devnexa-server-{s['id']}" and ssh[4] == "--"

    d = ssh_dirs / "srv" / str(s["id"])
    assert (d / "key").read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY")
    assert oct(os.stat(d / "key").st_mode)[-3:] == "600"
    cfg = (d / "config").read_text()
    assert "HostName 203.0.113.10" in cfg and "User ubuntu" in cfg and "StrictHostKeyChecking accept-new" in cfg
    user_cfg = (ssh_dirs / "home" / ".ssh" / "config").read_text()
    assert user_cfg.count(f"Include {ssh_dirs / 'srv' / 'ssh_config'}") == 1
    with SessionLocal() as db:
        servers.materialize(servers.get(db, s["id"]))
    assert (ssh_dirs / "home" / ".ssh" / "config").read_text() == user_cfg  # included once


def test_check_explains_ssh_failures(client, ssh_dirs, monkeypatch):
    monkeypatch.setattr(deploy, "executor", RemoteFake(
        ssh_out="ubuntu@203.0.113.10: Permission denied (publickey).", ssh_code=255))
    s = add(client, "denied-test")
    r = client.post(f"/api/servers/{s['id']}/check").json()
    assert r["status"] == "offline" and "authorize command" in r["error"]
    # an offline server cannot take deploys
    resp = client.post("/api/apps/deploy", json={"repo": "v/shop", "server_id": s["id"]})
    assert resp.status_code == 409


def test_no_docker_status(client, ssh_dirs, monkeypatch):
    out = PROBE_OK.replace("docker=27.3.1", "docker=").replace(
        "docker_error=", "docker_error=permission denied while trying to connect to the Docker daemon socket")
    monkeypatch.setattr(deploy, "executor", RemoteFake(ssh_out=out))
    s = add(client, "nodocker-test")
    r = client.post(f"/api/servers/{s['id']}/check").json()
    assert r["status"] == "no_docker" and "cannot use it" in r["error"]


def test_exec_and_container_actions(client, ssh_dirs, monkeypatch):
    ex = RemoteFake(ssh_out="hello")
    monkeypatch.setattr(deploy, "executor", ex)
    s = add(client, "exec-test")
    r = client.post(f"/api/servers/{s['id']}/exec", json={"command": "echo hello"}).json()
    assert r == {"exit_code": 0, "output": "hello", "command": "echo hello"}
    assert ex.calls[-1][-1] == "echo hello"
    r = client.post(f"/api/servers/{s['id']}/containers/postgres/restart", json={})
    assert r.status_code == 200 and ex.calls[-1][-1] == "docker restart postgres"
    assert client.post(f"/api/servers/{s['id']}/containers/x;rm -rf ~/restart", json={}).status_code in (400, 404)
    assert client.post(f"/api/servers/{s['id']}/containers/postgres/explode", json={}).status_code == 404
    audit = client.get("/api/audit").json()
    rows = audit if isinstance(audit, list) else audit.get("items", [])
    assert any(a.get("action") == "server.exec" for a in rows)


def test_install_docker_job(client, ssh_dirs, monkeypatch):
    monkeypatch.setattr(deploy, "executor", RemoteFake())
    s = add(client, "install-test")
    assert client.post(f"/api/servers/{s['id']}/install-docker", json={}).status_code == 202
    got = client.get(f"/api/servers/{s['id']}").json()
    assert got["job"]["status"] == "succeeded" and got["status"] == "online"


def test_deploy_to_remote_server(client, ssh_dirs, fake, monkeypatch):  # noqa: F811
    ex = RemoteFake(files={"requirements.txt": "flask\n", "app.py": "from flask import Flask\napp = Flask(__name__)\n"})
    monkeypatch.setattr(deploy, "executor", ex)
    probed = []
    monkeypatch.setattr(deploy, "probe", lambda url: probed.append(url) or (200, 4.0))
    s = add(client, "edge-1", address="apps.example.net", port_base=31000)
    client.post(f"/api/servers/{s['id']}/check")

    d = deploy_repo(client, repo="vaibhav/remote-shop", server_id=s["id"])
    assert d["status"] == "running" and d["options"]["server_id"] == s["id"]
    assert "Target server: edge-1" in d["build_log"]
    # every docker command of the deploy went to the remote engine
    assert {t for _, t in ex.targets} == {"edge-1"}
    run = next(c for c in ex.calls if c[:2] == ["docker", "run"])
    host_port = 31000 + d["id"] % 1000
    assert "--network" not in run and f"{host_port}:8000" in run
    assert probed[0] == f"http://apps.example.net:{host_port}/"
    assert d["url"] == "https://remote-shop.devnexa.space" and d["host_port"] == host_port
    # the DevNexa host's proxy sends the app's traffic to the remote server
    r = client.get("/api/apps/route", headers={"X-Forwarded-Host": "remote-shop.devnexa.space"})
    assert r.headers["x-devnexa-upstream"] == f"apps.example.net:{host_port}"

    # monitoring talks to the same server
    ex.targets.clear()
    deploy.monitor_tick()
    assert ("inspect", "edge-1") in ex.targets

    # redeploys stay on that server; the server cannot be removed while the app runs there
    r = client.post(f"/api/apps/deployments/{d['id']}/redeploy").json()
    assert r["options"]["server_id"] == s["id"]
    assert client.delete(f"/api/servers/{s['id']}").status_code == 409
    assert "remote-shop" in client.get(f"/api/servers/{s['id']}").json()["apps"]

    # stopping removes the container on the remote engine
    live = client.get("/api/apps").json()
    live = next(a for a in live if a["app"] == "remote-shop")["live"]
    ex.targets.clear()
    client.post(f"/api/apps/deployments/{live['id']}/stop")
    assert ("rm", "edge-1") in ex.targets
    assert client.delete(f"/api/servers/{s['id']}").status_code == 200


def test_executor_points_docker_at_the_server(monkeypatch):
    seen = []

    class P:
        def __init__(self, cmd, env=None, **kw):
            seen.append((cmd[0], (env or {}).get("DOCKER_HOST")))
            self.stdout, self.returncode = iter(()), 0

        def wait(self):
            return 0

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(deploy.subprocess, "Popen", P)
    token = servers._target.set({"id": 7, "name": "x", "docker_host": "ssh://devnexa-server-7", "address": "h",
                                 "port_base": 20000})
    try:
        deploy.Executor().run(["docker", "ps"])
        deploy.Executor().run(["git", "status"])
    finally:
        servers._target.reset(token)
    deploy.Executor().run(["docker", "info"])
    assert seen == [("docker", "ssh://devnexa-server-7"), ("git", None), ("docker", None)]
