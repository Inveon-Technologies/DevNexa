"""Sign in with GitHub: the OAuth web flow, a signed session cookie, user accounts and roles.

Many people can sign in. Each one is a `User` row: a member sees and deploys only their own repos, apps, servers and
backups; an admin sees and manages everything and approves new members. Who gets in is DEVNEXA_GITHUB_SIGNUP:
"approval" (default: anyone with GitHub, but an admin approves them first), "open" or "invite". Admins are
DEVNEXA_GITHUB_ADMINS, or else the server owner: the first login in DEVNEXA_GITHUB_ALLOWED_USERS, or the first
account that ever signed in. Each user's OAuth token is kept server side (kv_store) and never sent to the browser."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import KeyValue, User

COOKIE = "devnexa_session"
STATE_COOKIE = "devnexa_oauth_state"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
# repo: list, clone and push fix branches / open PRs on private and public repos. read:user: profile.
SCOPES = "repo read:user"
OWNER_KEY = "auth_owner"
SECRET_KEY = "session_secret"
SIGNUP_MODES = ("approval", "open", "invite")


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


def session_login(request: Request, db: Session) -> str | None:
    """The login in a valid session cookie, whatever that account's status is now."""
    data = verify(db, request.cookies.get(COOKIE))
    return data["login"] if data and data.get("login") else None


def current_user(request: Request, db: Session) -> str | None:
    """The signed-in login, if that account may use DevNexa right now (approved and not blocked)."""
    login = session_login(request, db)
    return login if login and is_active(db, login) else None


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


def _logins(value: str) -> list[str]:
    return [u.strip().lower().lstrip("@") for u in value.split(",") if u.strip()]


def allowed_list() -> set[str]:
    return set(_logins(get_settings().github_allowed_users))


def admin_list() -> set[str]:
    return set(_logins(get_settings().github_admins))


def signup_mode() -> str:
    mode = get_settings().github_signup.strip().lower()
    return mode if mode in SIGNUP_MODES else "approval"


def owner(db: Session) -> str | None:
    """The server owner: the account that set DevNexa up. An admin unless DEVNEXA_GITHUB_ADMINS names others."""
    row = db.get(KeyValue, OWNER_KEY)
    if row:
        return row.value.get("login")
    listed = _logins(get_settings().github_allowed_users)
    return listed[0] if listed and not admin_list() else None


def get_user(db: Session, login: str | None) -> User | None:
    return db.get(User, login.lower()) if login else None


def is_admin(db: Session, login: str | None) -> bool:
    if not login:
        return False
    login = login.lower()
    if login in admin_list():
        return True
    first = owner(db)
    if first and first.lower() == login:
        return True
    u = db.get(User, login)
    return bool(u and u.role == "admin" and u.status == "active")


def is_built_in_admin(db: Session, login: str | None) -> bool:
    """Admins from .env and the server owner: they cannot be blocked or demoted from the Users page."""
    login = (login or "").lower()
    return bool(login) and (login in admin_list() or login == (owner(db) or "").lower())


def is_active(db: Session, login: str) -> bool:
    if is_admin(db, login):
        return True
    u = get_user(db, login)
    return bool(u and u.status == "active")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def admit(db: Session, login: str, profile: dict | None = None) -> User | None:
    """Sign-in decision for a GitHub account that just authenticated. Returns its User row (status active, pending
    or blocked), or None when it may not sign in at all (invite-only and not invited)."""
    profile = profile or {}
    key = login.lower()
    u = db.get(User, key)
    if not u and not admin_list() and not owner(db):
        db.add(KeyValue(key=OWNER_KEY, value={"login": key}))  # the very first account owns the server
        db.flush()
    admin = is_admin(db, key)
    if not u:
        mode = signup_mode()
        if admin or key in allowed_list() or mode == "open":
            status = "active"
        elif mode == "approval":
            status = "pending"
        else:
            return None
        u = User(login=key, display_login=login, role="admin" if admin else "member", status=status)
        db.add(u)
    elif u.status == "pending" and (key in allowed_list() or signup_mode() == "open"):
        u.status = "active"
    if is_built_in_admin(db, key):
        u.role, u.status = "admin", "active"
    u.display_login = profile.get("login") or login
    u.name, u.avatar_url = profile.get("name"), profile.get("avatar_url")
    u.last_login_at = _now()
    db.flush()
    return u


def is_allowed(db: Session, login: str, claim: bool = True) -> bool:
    """Whether `login` may use DevNexa now (kept for callers that only need yes or no)."""
    if claim:
        u = admit(db, login)
        return bool(u and u.status == "active")
    return is_active(db, login)
