"""Many GitHub users: sign-up approval, roles, and each user seeing only their own repos, apps, servers and backups."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import AppBackup, AppDeployment, AppOwner, KeyValue, Server
from app.services import github, owners

from test_auth import oauth, sign_in  # noqa: F401  (fixture)


@pytest.fixture
def people(client, oauth):  # noqa: F811
    """Signed-in clients: the owner (admin), an approved member and someone waiting for approval."""
    with SessionLocal() as db:
        for model in (AppOwner, AppBackup):
            db.query(model).delete()
        db.query(AppDeployment).filter(AppDeployment.app.in_(["alice-shop", "owner-blog", "alice-shop-pr-1"])).delete()
        db.query(Server).filter(Server.name.in_(["owner-box", "alice-box"])).delete()
        db.add(KeyValue(key=github.KV_KEY, value={"token": "ghp_owner_shared", "login": "vaibhavpetkar"})) \
            if not db.get(KeyValue, github.KV_KEY) else None
        db.commit()
    owner = client
    sign_in(owner, "vaibhavpetkar")
    alice, bob = TestClient(app), TestClient(app)
    sign_in(alice, "alice")
    sign_in(bob, "bob")
    assert owner.patch("/api/users/alice", json={"status": "active"}).status_code == 200
    yield owner, alice, bob
    with SessionLocal() as db:
        row = db.get(KeyValue, github.KV_KEY)
        if row:
            db.delete(row)
            db.commit()


def _app(db, name, owner_login, **opts):
    d = AppDeployment(app=name, repo=f"{owner_login or 'x'}/{name}", branch="main", status="running",
                      health="healthy", options=opts)
    db.add(d)
    if owner_login:
        db.add(AppOwner(app=name, owner=owner_login))
    db.flush()
    return d.id


def test_new_accounts_wait_for_approval(people):
    owner, alice, bob = people
    me = bob.get("/api/auth/me").json()
    assert me["signed_in"] is False and me["account_status"] == "pending" and me["pending_login"] == "bob"
    r = bob.get("/api/apps")
    assert r.status_code == 403 and "approve" in r.json()["detail"]
    users = {u["key"]: u for u in owner.get("/api/users").json()["users"]}
    assert users["bob"]["status"] == "pending" and users["vaibhavpetkar"]["built_in_admin"]
    assert owner.get("/api/auth/me").json()["pending_users"] == 1
    # members cannot see or manage accounts
    assert alice.get("/api/users").status_code == 403
    assert owner.patch("/api/users/bob", json={"status": "active"}).json()["status"] == "active"
    assert bob.get("/api/apps").status_code == 200


def test_open_signup_lets_anyone_in(client, oauth, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "github_signup", "open")
    sign_in(client, "carol")
    me = client.get("/api/auth/me").json()
    assert me["signed_in"] and me["role"] == "member"


def test_blocking_ends_the_session_and_owner_cannot_be_blocked(people):
    owner, alice, _ = people
    assert alice.get("/api/apps").status_code == 200
    assert owner.patch("/api/users/alice", json={"status": "blocked"}).status_code == 200
    assert alice.get("/api/apps").status_code == 403
    assert owner.patch("/api/users/vaibhavpetkar", json={"status": "blocked"}).status_code == 409
    owner.patch("/api/users/alice", json={"status": "active", "role": "admin"})
    assert alice.get("/api/auth/me").json()["role"] == "admin"
    assert len(alice.get("/api/users").json()["users"]) >= 3


def test_invited_login_can_sign_in_in_invite_mode(people, monkeypatch):
    owner, _, _ = people
    monkeypatch.setattr(get_settings(), "github_signup", "invite")
    assert owner.post("/api/users", json={"login": "@Dave"}).status_code == 201
    dave = TestClient(app)
    assert sign_in(dave, "Dave").headers["location"] == "/deploy"
    assert dave.get("/api/auth/me").json()["signed_in"]


def test_members_use_only_their_own_github_token(people):
    owner, alice, _ = people
    with SessionLocal() as db:
        assert github.get_token(db, "alice") == "gho_token_for_alice"
        db.delete(db.get(KeyValue, github.user_key("alice")))
        db.commit()
        # no own token: a member never falls back to the owner's shared connection; an admin does
        assert github.get_token(db, "alice") == ""
        assert github.get_token(db, None) == "ghp_owner_shared"
    assert alice.get("/api/github").json() == {"connected": False}
    assert alice.get("/api/github/repos").status_code == 400
    assert owner.get("/api/github").json()["source"] == "oauth"
    # disconnecting as a member never touches the shared connection
    alice.delete("/api/github")
    with SessionLocal() as db:
        assert db.get(KeyValue, github.KV_KEY)


def test_apps_are_private_to_their_owner(people):
    owner, alice, bob = people
    owner.patch("/api/users/bob", json={"status": "active"})
    with SessionLocal() as db:
        a = _app(db, "alice-shop", "alice")
        pr = _app(db, "alice-shop-pr-1", None, preview_of="alice-shop")
        o = _app(db, "owner-blog", None)  # from before multi-user sign-in: the admins'
        db.add(AppBackup(app="alice-shop", status="trashed", label="old"))
        db.commit()

    assert {x["app"] for x in alice.get("/api/apps").json()} == {"alice-shop"}
    assert {x["app"] for x in alice.get("/api/live/apps").json()} == {"alice-shop"}
    assert alice.get(f"/api/apps/deployments/{a}").status_code == 200
    assert alice.get(f"/api/apps/deployments/{pr}").status_code == 200  # her test URL
    assert alice.get(f"/api/apps/deployments/{o}").status_code == 404
    assert alice.get("/api/live/apps/owner-blog").status_code == 404
    assert alice.get("/api/live/apps/owner-blog/data").status_code == 404
    assert alice.post(f"/api/apps/deployments/{o}/stop").status_code == 404
    assert [x["app"] for x in alice.get("/api/live/trash").json()["backups"]] == ["alice-shop"]

    assert bob.get("/api/apps").json() == []
    assert bob.get("/api/live/apps/alice-shop").status_code == 404
    assert bob.get(f"/api/apps/deployments/{a}/logs/stream").status_code == 404
    assert bob.get("/api/live/trash").json()["backups"] == []
    assert bob.delete("/api/live/trash").json()["purged"] == 0

    # the admin sees everything, with who owns it
    mine = {x["app"]: x for x in owner.get("/api/apps").json()}
    assert {"alice-shop", "owner-blog"} <= set(mine) and mine["alice-shop"]["owner"] == "alice"
    assert owner.get("/api/live/apps/alice-shop").status_code == 200

    # app names are global: Bob cannot take Alice's name, or a name kept for test URLs
    r = bob.post("/api/apps/deploy", json={"repo": "bob/shop", "app": "alice-shop"})
    assert r.status_code == 409 and "another account" in r.json()["detail"]
    assert bob.post("/api/apps/deploy", json={"repo": "bob/shop", "app": "shop-pr-3"}).status_code == 400


def test_new_deployments_record_their_owner(people):
    from app.agents import deploy

    with SessionLocal() as db:
        deploy.create_deployment(db, "alice/thing", "main", "alice-thing", options={"gh_user": "Alice"})
        deploy.create_deployment(db, "alice/thing", "fix", "alice-thing-pr-9", options={"preview_of": "alice-thing"})
        db.commit()
        assert owners.app_owner(db, "alice-thing") == "alice"
        assert owners.app_owner(db, "alice-thing-pr-9") == "alice"
        with pytest.raises(owners.NameTaken):
            deploy.create_deployment(db, "bob/thing", "main", "alice-thing", options={"gh_user": "bob"})
        db.rollback()
        db.query(AppDeployment).filter(AppDeployment.app.like("alice-thing%")).delete(synchronize_session=False)
        db.query(AppOwner).filter(AppOwner.app.like("alice-thing%")).delete(synchronize_session=False)
        db.commit()


def test_app_quota(people, monkeypatch):
    _, alice, _ = people
    monkeypatch.setattr(get_settings(), "user_max_apps", 1)
    with SessionLocal() as db:
        _app(db, "alice-shop", "alice")
        db.commit()
    r = alice.post("/api/apps/deploy", json={"repo": "alice/other"})
    assert r.status_code == 403 and "1 apps" in r.json()["detail"]


def test_servers_are_private_to_their_owner(people):
    owner, alice, _ = people
    with SessionLocal() as db:
        db.add(Server(name="owner-box", host="10.0.0.1", private_key="k", public_key="p", owner=None))
        db.add(Server(name="alice-box", host="10.0.0.2", private_key="k", public_key="p", owner="alice"))
        db.commit()
        ids = {s.name: s.id for s in db.query(Server).filter(Server.name.in_(["owner-box", "alice-box"]))}
    assert [s["name"] for s in alice.get("/api/servers").json()] == ["alice-box"]
    assert alice.get(f"/api/servers/{ids['owner-box']}").status_code == 404
    assert alice.post(f"/api/servers/{ids['owner-box']}/exec", json={"command": "id"}).status_code == 404
    r = alice.post("/api/apps/deploy", json={"repo": "alice/x", "app": "alice-x", "server_id": ids["owner-box"]})
    assert r.status_code == 404
    assert {"owner-box", "alice-box"} <= {s["name"] for s in owner.get("/api/servers").json()}


def test_server_wide_settings_are_admin_only(people):
    owner, alice, _ = people
    assert alice.put("/api/policies/autonomy", json={}).status_code == 403
    assert alice.post("/api/environment/reset").status_code == 403
    assert owner.put("/api/policies/autonomy", json={}).status_code == 200


def test_audit_log_shows_members_only_their_own_actions(people):
    owner, alice, _ = people
    actors = {e["actor"].lower() for e in alice.get("/api/audit").json()}
    assert actors <= {"alice"}
    assert len({e["actor"] for e in owner.get("/api/audit").json()}) > 1


def test_missing_columns_are_added(tmp_path, monkeypatch):
    from app import db as dbmod

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    md = MetaData()
    Table("servers", md, Column("id", Integer, primary_key=True), Column("name", String(60)))
    md.create_all(eng)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod.Base.metadata.create_all(eng)  # makes the new tables; leaves the old servers table as it was
    assert "owner" not in {c["name"] for c in inspect(eng).get_columns("servers")}
    assert "servers.owner" in dbmod.add_missing_columns()
    assert "owner" in {c["name"] for c in inspect(eng).get_columns("servers")}
    assert dbmod.add_missing_columns() == []
