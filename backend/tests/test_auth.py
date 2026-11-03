from urllib.parse import parse_qs, urlparse

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import KeyValue, User
from app.services import auth, github


@pytest.fixture
def oauth(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "github_client_id", "Iv1.abc")
    monkeypatch.setattr(s, "github_client_secret", "shh")
    monkeypatch.setattr(s, "public_url", "http://testserver")
    monkeypatch.setattr(s, "github_allowed_users", "VaibhavPetkar")
    monkeypatch.setattr(s, "github_admins", "")
    monkeypatch.setattr(s, "github_signup", "approval")
    with SessionLocal() as db:  # every test starts with no accounts and no owner
        db.query(User).delete()
        row = db.get(KeyValue, auth.OWNER_KEY)
        if row:
            db.delete(row)
        db.commit()
    monkeypatch.setattr(auth, "exchange_code", lambda request, code: f"gho_token_for_{code}")
    monkeypatch.setattr(github, "profile", lambda token: ({"login": token.removeprefix("gho_token_for_"),
                                                           "name": "V", "avatar_url": "a"}, "repo, read:user"))
    seen = {}
    monkeypatch.setattr(github, "list_repos", lambda token: seen.setdefault("token", token) and [])
    client.cookies.clear()
    yield seen
    client.cookies.clear()


def sign_in(client, login):
    r = client.get("/api/auth/github/login?next=/deploy", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == ["http://testserver/api/auth/github/callback"]
    assert q["scope"] == ["repo read:user"] and q["client_id"] == ["Iv1.abc"]
    return client.get(f"/api/auth/github/callback?code={login}&state={q['state'][0]}", follow_redirects=False)


def test_sign_in_with_github_unlocks_deploys_with_the_users_token(client, oauth):
    assert client.get("/api/apps").status_code == 401
    assert client.get("/api/auth/me").json() == {"signed_in": False, "login": None, "github_login": True,
                                                 "admin_key": False, "signup": "approval"}
    r = sign_in(client, "vaibhavpetkar")
    assert r.status_code == 302 and r.headers["location"] == "/deploy"
    me = client.get("/api/auth/me").json()
    assert me["signed_in"] and me["login"] == "vaibhavpetkar"
    assert me["role"] == "admin"  # the first allow-listed login is the server owner
    assert client.get("/api/apps").status_code == 200
    assert client.get("/api/github").json()["source"] == "oauth"
    client.get("/api/github/repos")
    assert oauth["token"] == "gho_token_for_vaibhavpetkar"
    assert "gho_token" not in client.get("/api/github").text  # the token never reaches the browser
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/apps").status_code == 401


def test_invite_only_refuses_accounts_not_on_the_allow_list(client, oauth, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_signup", "invite")
    r = sign_in(client, "stranger")
    assert "login_error=" in r.headers["location"] and "stranger" in r.headers["location"]
    assert client.get("/api/apps").status_code == 401
    with SessionLocal() as db:
        assert db.get(KeyValue, github.user_key("stranger")) is None
        assert db.get(User, "stranger") is None


def test_state_must_match(client, oauth):
    client.get("/api/auth/github/login", follow_redirects=False)
    r = client.get("/api/auth/github/callback?code=vaibhavpetkar&state=forged", follow_redirects=False)
    assert "login_error=" in r.headers["location"]
    assert client.get("/api/auth/me").json()["signed_in"] is False


def test_first_account_becomes_owner_without_allow_list(client, oauth, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_allowed_users", "")
    assert sign_in(client, "firstuser").headers["location"] == "/deploy"
    assert client.get("/api/auth/me").json()["role"] == "admin"
    client.cookies.clear()
    sign_in(client, "seconduser")  # approval mode: they get in line, they are not the owner
    me = client.get("/api/auth/me").json()
    assert not me["signed_in"] and me["account_status"] == "pending"


def test_tampered_session_cookie_is_rejected(client, oauth):
    sign_in(client, "vaibhavpetkar")
    value = client.cookies.get(auth.COOKIE)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, value[:-3] + "abc")
    assert client.get("/api/apps").status_code == 401
