"""Sign in with GitHub: the OAuth web flow, a signed session cookie, and who is allowed in.

Deploying runs code on the server, so only allowed GitHub accounts may sign in: the logins listed in
DEVNEXA_GITHUB_ALLOWED_USERS, or, when that list is empty, the first account that ever signs in (the owner).
The OAuth token of each user is kept server side (kv_store) and never sent to the browser."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import KeyValue

COOKIE = "devnexa_session"
STATE_COOKIE = "devnexa_oauth_state"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
# repo: list, clone and push fix branches / open PRs on private and public repos. read:user: profile.
SCOPES = "repo read:user"
OWNER_KEY = "auth_owner"
SECRET_KEY = "session_secret"


class AuthError(Exception):
    pass


def enabled() -> bool:
    s = get_settings()
    return bool(s.github_client_id and s.github_client_secret)


def _secret(db: Session) -> bytes:
    s = get_settings()
    if s.session_secret:
        return s.session_secret.encode()
    row = db.get(KeyValue, SECRET_KEY)
    if not row:  # generated once and kept in the database, so sessions survive restarts
        row = KeyValue(key=SECRET_KEY, value={"secret": secrets.token_urlsafe(48)})
        db.add(row)
        db.commit()
    return row.value["secret"].encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(db: Session, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(_secret(db), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def verify(db: Session, value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    body, mac = value.rsplit(".", 1)
    good = _b64(hmac.new(_secret(db), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, good):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    return data if data.get("exp", 0) > time.time() else None


def session_cookie(db: Session, login: str) -> tuple[str, int]:
    max_age = get_settings().session_days * 86400
    return sign(db, {"login": login, "exp": int(time.time()) + max_age}), max_age


def current_user(request: Request, db: Session) -> str | None:
    data = verify(db, request.cookies.get(COOKIE))
    return data["login"] if data and is_allowed(db, data["login"], claim=False) else None


def base_url(request: Request) -> str:
    s = get_settings()
    if s.public_url:
        return s.public_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def callback_url(request: Request) -> str:
    return base_url(request) + "/api/auth/github/callback"


def authorize_url(request: Request, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": get_settings().github_client_id, "redirect_uri": callback_url(request),
        "scope": SCOPES, "state": state, "allow_signup": "false"})


def exchange_code(request: Request, code: str) -> str:
    s = get_settings()
    try:
        r = httpx.post(TOKEN_URL, headers={"Accept": "application/json"}, timeout=20.0, data={
            "client_id": s.github_client_id, "client_secret": s.github_client_secret, "code": code,
            "redirect_uri": callback_url(request)})
    except httpx.HTTPError as e:
        raise AuthError(f"GitHub is unreachable: {e}") from e
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("access_token"):
        raise AuthError(data.get("error_description") or data.get("error") or f"GitHub returned {r.status_code}")
    return data["access_token"]


def allowed_list() -> set[str]:
    return {u.strip().lower() for u in get_settings().github_allowed_users.split(",") if u.strip()}


def owner(db: Session) -> str | None:
    row = db.get(KeyValue, OWNER_KEY)
    return row.value.get("login") if row else None


def is_allowed(db: Session, login: str, claim: bool = True) -> bool:
    login = login.lower()
    listed = allowed_list()
    if listed:
        return login in listed
    first = owner(db)
    if first:
        return first.lower() == login
    if claim:  # nobody configured and nobody signed in yet: this account becomes the owner
        db.add(KeyValue(key=OWNER_KEY, value={"login": login}))
        db.flush()
        return True
    return False
