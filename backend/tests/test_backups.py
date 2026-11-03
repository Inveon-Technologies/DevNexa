"""Smart data backups, managed databases, the trash, and backup import/download."""

import gzip
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agents import deploy
from app.config import get_settings
from app.services import backups

from test_deploy import FakeExecutor, fake  # noqa: F401  (fixture)
from test_live import FILES, deploy_app


class DataExecutor(FakeExecutor):
    """FakeExecutor that also plays the backup helper containers: a dump writes `self.data` (the /data volume)
    or `self.sql` (the database) to the OUT path, and restores are recorded."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.data = {"uploads/a.txt": "hello"}
        self.sql = "-- PostgreSQL database dump\nCREATE TABLE t (id int);\n\\restrict abc123\n"
        self.restores = []

    def run(self, cmd, on_line=None, timeout=None, env=None):
        envs = dict(c.split("=", 1) for c in cmd[cmd.index("-e") + 1::] if "=" in c and c.split("=")[0] in
                    ("OUT", "IN", "DB_URL")) if "-e" in cmd else {}
        if cmd[:3] == ["docker", "run", "--rm"] and "OUT" in envs:
            out = Path(get_settings().backups_dir) / envs["OUT"].removeprefix("/backups/")
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.name == "data.tar.gz":
                with tarfile.open(out, "w:gz") as tf:
                    for name, text in self.data.items():
                        info = tarfile.TarInfo(name)
                        info.size = len(text.encode())
                        tf.addfile(info, io.BytesIO(text.encode()))
            else:
                out.write_bytes(gzip.compress(self.sql.encode()))
            self.calls.append(cmd)
            return 0, ""
        if cmd[:3] == ["docker", "run", "--rm"] and "IN" in envs:
            self.restores.append((envs["IN"], cmd[-4]))
        return super().run(cmd, on_line, timeout, env)


@pytest.fixture
def data_fake(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "backups_dir", str(tmp_path / "backups"))
    fake(files=FILES)
    ex = DataExecutor(files=FILES)
    monkeypatch.setattr(deploy, "executor", ex)
    return ex


def data(client, app):
    return client.get(f"/api/live/apps/{app}/data").json()


def test_app_gets_a_persistent_data_volume_and_a_database_url(client, data_fake):
    ex = data_fake
    d = deploy_app(client, "vol-app")
    run = next(c for c in ex.calls if c[:2] == ["docker", "run"] and d["container"] in c)
    assert "devnexa-data-vol-app:/data" in run and "DATA_DIR=/data" in run

    r = client.post("/api/live/apps/vol-app/databases", json={"engine": "postgres"})
    assert r.status_code == 201, r.text
    db = r.json()["database"]
    assert db["status"] == "running" and "••••••" in db["url_masked"]
    new = client.get(f"/api/apps/deployments/{r.json()['deployment']['id']}").json()
    assert new["status"] == "running" and new["image"] == d["image"]  # same image, no rebuild
    run = next(c for c in ex.calls if c[:2] == ["docker", "run"] and new["container"] in c)
    assert any(x.startswith("DATABASE_URL=postgresql://vol_app:") and db["container"] in x for x in run)
    assert client.post("/api/live/apps/vol-app/databases", json={"engine": "mysql"}).status_code == 409


def test_smart_backup_holds_data_and_database_and_skips_when_unchanged(client, data_fake):
    ex = data_fake
    deploy_app(client, "bk-app")
    client.post("/api/live/apps/bk-app/databases", json={"engine": "postgres", "restart_app": False})
    ov = data(client, "bk-app")
    assert {s["type"] for s in ov["sources"]} == {"data", "db"} and all("url" not in s for s in ov["sources"])

    assert "bk-app" in backups.run_due()
    b = data(client, "bk-app")["backups"][0]
    assert b["kind"] == "scheduled" and b["status"] == "ready" and b["verified"]
    assert {p["type"] for p in b["parts"]} == {"data", "db"} and b["release_label"] == "v1"
    assert "bk-app" not in backups.run_due()  # not due again for 24 h

    later = datetime.now(timezone.utc) + timedelta(hours=25)
    ex.sql = ex.sql.replace("abc123", "zzz999")  # only a volatile line differs: same content
    backups.run_due(later)
    assert len(data(client, "bk-app")["backups"]) == 1
    ex.data["uploads/b.txt"] = "new file"
    backups.run_due(later + timedelta(hours=25))
    assert len(data(client, "bk-app")["backups"]) == 2


def test_new_version_is_preceded_by_a_data_backup(client, data_fake):
    deploy_app(client, "pre-app")
    d2 = deploy_app(client, "pre-app")
    b = data(client, "pre-app")["backups"]
    assert b[0]["kind"] == "pre-deploy" and b[0]["release_label"] == "v1"
    assert "[backup] data backed up as" in d2["build_log"]


def test_retention_keeps_tiers_and_moves_the_rest_to_trash(client, data_fake):
    t0 = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    items = [(i, t0 - timedelta(hours=12 * i)) for i in range(40)]  # two a day for 20 days
    keep = backups.plan_retention(items, keep_last=3, keep_daily=5, keep_weekly=2, keep_monthly=1)
    assert {0, 1, 2} <= keep
    assert len({items[i][1].date() for i in keep}) == len(keep) - 1  # 0 and 1 share a day: one extra
    assert max(keep) < 40 and 39 not in keep

    deploy_app(client, "ret-app")
    client.put("/api/live/apps/ret-app/settings", json={"backup_policy": {"keep_last": 1, "keep_daily": 0,
                                                                          "keep_weekly": 0, "keep_monthly": 0}})
    for i in range(3):
        data_fake.data[f"f{i}"] = "x"
        client.post("/api/live/apps/ret-app/data-backups", json={})
    ov = data(client, "ret-app")
    assert len([b for b in ov["backups"] if b["status"] == "ready"]) == 1 and ov["trash_count"] == 2
    t = client.get("/api/live/trash?app=ret-app").json()
    assert all(b["trash_reason"] == "retention" for b in t["backups"])


def test_trash_restore_purge_and_expiry(client, data_fake):
    deploy_app(client, "tr-app")
    b = client.post("/api/live/apps/tr-app/data-backups", json={}).json()
    folder = Path(get_settings().backups_dir) / "tr-app" / str(b["id"])
    assert (folder / "manifest.json").is_file()

    assert client.delete(f"/api/live/apps/tr-app/data-backups/{b['id']}").json()["status"] == "trashed"
    assert not folder.exists() and (Path(get_settings().backups_dir) / ".trash/tr-app" / str(b["id"])).is_dir()
    r = client.post(f"/api/live/trash/backup/{b['id']}/restore", json={})
    assert r.json()["status"] == "ready" and folder.is_dir()

    client.delete(f"/api/live/apps/tr-app/data-backups/{b['id']}")
    assert backups.purge_expired(datetime.now(timezone.utc) + timedelta(days=15)) >= 1
    assert data(client, "tr-app")["trash_count"] == 0
    assert not (Path(get_settings().backups_dir) / ".trash/tr-app" / str(b["id"])).exists()


def test_deleting_a_database_takes_a_final_backup_and_keeps_it_in_trash(client, data_fake):
    ex = data_fake
    deploy_app(client, "dbt-app")
    d = client.post("/api/live/apps/dbt-app/databases", json={"engine": "postgres", "restart_app": False}).json()
    db_id = d["database"]["id"]
    r = client.delete(f"/api/live/apps/dbt-app/databases/{db_id}?restart_app=false").json()
    assert r["database"]["status"] == "trashed" and r["final_backup_id"]
    final = next(b for b in data(client, "dbt-app")["backups"] if b["id"] == r["final_backup_id"])
    assert final["kind"] == "final" and final["pinned"] and [p["type"] for p in final["parts"]] == ["db"]
    assert ["docker", "volume", "rm", d["database"]["volume"]] not in ex.calls
    assert data(client, "dbt-app")["database"] is None

    r = client.post(f"/api/live/trash/database/{db_id}/restore", json={})
    assert r.status_code == 200 and r.json()["database"]["status"] == "running"
    client.delete(f"/api/live/apps/dbt-app/databases/{db_id}?restart_app=false")
    assert client.delete(f"/api/live/trash/database/{db_id}").status_code == 200
    assert ["docker", "volume", "rm", d["database"]["volume"]] in ex.calls


def test_restore_takes_a_safety_backup_first_and_checks_engines(client, data_fake):
    ex = data_fake
    deploy_app(client, "rs-app")
    client.post("/api/live/apps/rs-app/databases", json={"engine": "postgres", "restart_app": False})
    b = client.post("/api/live/apps/rs-app/data-backups", json={}).json()
    ex.data["changed.txt"] = "after"
    r = client.post(f"/api/live/apps/rs-app/data-backups/{b['id']}/restore", json={}).json()
    assert sorted(r["restored"]) == ["data", "db"] and r["errors"] == [] and r["safety_backup_id"]
    assert {img for _, img in ex.restores} == {"alpine:3.20", "postgres:16-alpine"}
    assert any(c[:2] == ["docker", "restart"] for c in ex.calls)
    safety = next(x for x in data(client, "rs-app")["backups"] if x["id"] == r["safety_backup_id"])
    assert safety["kind"] == "pre-restore"

    # a MySQL dump cannot go into a PostgreSQL database
    up = client.post("/api/live/apps/rs-app/data-backups/import?filename=dump.sql&engine=mysql", content=b"CREATE TABLE x;")
    assert up.status_code == 201, up.text
    r = client.post(f"/api/live/apps/rs-app/data-backups/{up.json()['id']}/restore", json={"parts": ["db"]})
    assert r.status_code == 409 and "mysql" in r.json()["detail"]


def test_import_formats_and_download_roundtrip(client, data_fake):
    deploy_app(client, "im-app")
    sql = b"-- PostgreSQL database dump\nCREATE TABLE a (id int);\n"
    r = client.post("/api/live/apps/im-app/data-backups/import?filename=prod.sql", content=sql)
    assert r.status_code == 201, r.text
    assert r.json()["parts"][0] == {**r.json()["parts"][0], "type": "db", "engine": "postgres"}
    assert r.json()["kind"] == "imported" and r.json()["pinned"]

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in (("site/index.html", "<h1>x</h1>"), ("../evil.txt", "no")):
            info = tarfile.TarInfo(name)
            info.size = len(text)
            tf.addfile(info, io.BytesIO(text.encode()))
        link = tarfile.TarInfo("passwd")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(link)
    r = client.post("/api/live/apps/im-app/data-backups/import?filename=files.tar.gz", content=buf.getvalue())
    assert r.status_code == 201, r.text
    stored = Path(get_settings().backups_dir) / "im-app" / str(r.json()["id"]) / "data.tar.gz"
    with tarfile.open(stored) as tf:
        assert tf.getnames() == ["site/index.html"]  # ../ escapes and links are dropped

    assert client.post("/api/live/apps/im-app/data-backups/import?filename=notes.txt", content=b"hi").status_code == 400
    assert client.post("/api/live/apps/im-app/data-backups/import?filename=x.sql.gz", content=b"not gzip").status_code == 400

    # download a backup and import it again (as on another server)
    b = client.post("/api/live/apps/im-app/data-backups", json={}).json()
    dl = client.get(f"/api/live/apps/im-app/data-backups/{b['id']}/download")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(dl.content)) as tf:
        assert json.load(tf.extractfile("manifest.json"))["format"] == "devnexa-backup/1"
    r = client.post("/api/live/apps/im-app/data-backups/import?filename=bundle.tar.gz&restore_now=true",
                    content=dl.content)
    assert r.status_code == 201, r.text
    assert [p["type"] for p in r.json()["parts"]] == ["data"] and r.json()["restore"]["restored"] == ["data"]


def test_switching_smart_backups_and_persistent_data_off(client, data_fake):
    ex = data_fake
    deploy_app(client, "off-app")
    client.put("/api/live/apps/off-app/settings", json={"steps": {"smart_backup": False, "persistent_data": False}})
    assert "off-app" not in backups.run_due()
    d = deploy_app(client, "off-app")
    run = next(c for c in ex.calls if c[:2] == ["docker", "run"] and d["container"] in c)
    assert not any("/data" in x for x in run)
    r = client.post("/api/live/apps/off-app/data-backups", json={})
    assert r.status_code == 400
