"""Sign in with GitHub (OAuth web flow) and the current session."""

import secrets
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import User
from ..services import auth, github
from ..services.audit import audit

router = APIRouter(prefix="/api/auth", tags=["sign in"])


def _safe_next(path: str | None) -> str:
    return path if path and path.startswith("/") and not path.startswith("//") else "/deploy"


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    login = auth.session_login(request, db)
    active = bool(login) and auth.is_active(db, login)
    u = auth.get_user(db, login)
    out = {"signed_in": active, "login": login if active else None, "github_login": auth.enabled(),
           "admin_key": bool(s.admin_token), "signup": auth.signup_mode()}
    if login and not active:
        out["account_status"] = u.status if u else "refused"
        out["pending_login"] = login
    if active:
        conn = github.connection(db, login)
        admin = auth.is_admin(db, login)
        out.update(name=conn.get("name") or (u.name if u else None),
                   avatar_url=conn.get("avatar_url") or (u.avatar_url if u else None),
                   role="admin" if admin else "member", is_admin=admin, account_status="active")
        if admin:
            out["pending_users"] = db.scalar(select(func.count()).select_from(User).where(User.status == "pending"))
    return out


@router.get("/github/login")
def github_login(request: Request, next: str | None = None, db: Session = Depends(get_db)):
    if not auth.enabled():
        return JSONResponse({"detail": "Sign in with GitHub is not set up: add DEVNEXA_GITHUB_CLIENT_ID and "
                                       "DEVNEXA_GITHUB_CLIENT_SECRET to .env."}, 503)
    state = secrets.token_urlsafe(24)
    r = RedirectResponse(auth.authorize_url(request, state), 302)
    secure = auth.base_url(request).startswith("https://")
    r.set_cookie(auth.STATE_COOKIE, auth.sign(db, {"state": state, "next": _safe_next(next), "exp": int(time.time()) + 600}),
                 max_age=600, httponly=True, samesite="lax", secure=secure, path="/api/auth")
    return r


def _back(path: str, error: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(path + (f"?login_error={quote(error)}" if error else ""), 302)


@router.get("/github/callback")
def github_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    db: Session = Depends(get_db)):
    saved = auth.verify(db, request.cookies.get(auth.STATE_COOKIE)) or {}
    nxt = _safe_next(saved.get("next"))
    if error:
        return _back(nxt, "GitHub sign-in was cancelled.")
    if not code or not state or not secrets.compare_digest(state, saved.get("state", "")):
        return _back(nxt, "Sign-in expired or was started in another browser. Try again.")
    try:
        token = auth.exchange_code(request, code)
        prof = github.profile(token)[0]
        login = prof["login"]
        account = auth.admit(db, login, prof)
        if account and account.status != "blocked":
            github.save_user_token(db, token)
    except (auth.AuthError, github.GitHubError) as e:
        db.rollback()
        return _back(nxt, f"GitHub sign-in failed: {e}")
    if not account or account.status == "blocked":
        audit(db, login, "auth.refused", "github", None, f"@{login} is not allowed to sign in")
        db.commit()
        why = "was blocked by an admin" if account else "is not invited to this DevNexa server. Ask an admin to add you"
        return _back(nxt, f"@{login} {why}.")
    if account.status == "pending":
        audit(db, login, "auth.pending", "user", login.lower(), f"@{login} signed up and is waiting for approval")
    else:
        audit(db, login, "auth.login", "github", None, f"@{login} signed in with GitHub")
    db.commit()
    value, max_age = auth.session_cookie(db, login)
    r = _back(nxt)
    r.delete_cookie(auth.STATE_COOKIE, path="/api/auth")
    r.set_cookie(auth.COOKIE, value, max_age=max_age, httponly=True, samesite="lax",
                 secure=auth.base_url(request).startswith("https://"), path="/")
    return r


@router.post("/logout")
def logout():
    r = JSONResponse({"signed_in": False})
    r.delete_cookie(auth.COOKIE, path="/")
    return r


# ------------------------------------------------------------------ users (admins)
def _require_admin(request: Request, db: Session = Depends(get_db)):
    from .apps import require_admin

    require_admin(request, db)


users = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(_require_admin)])


def user_dict(db: Session, u: User, counts: dict) -> dict:
    c = counts.get(u.login, {"apps": 0, "servers": 0})
    admin = auth.is_admin(db, u.login)
    return {"login": u.display_login or u.login, "key": u.login, "name": u.name, "avatar_url": u.avatar_url,
            "role": "admin" if admin else "member", "status": u.status, "built_in_admin": auth.is_built_in_admin(db, u.login),
            "apps": c["apps"], "servers": c["servers"], "note": u.note, "decided_by": u.decided_by,
            "github_connected": bool(github.connection(db, u.login).get("connected")) if u.status != "blocked" else False,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


@users.get("")
def list_users(db: Session = Depends(get_db)):
    from ..services import owners

    counts = owners.counts(db)
    rows = db.scalars(select(User)).all()
    order = {"pending": 0, "active": 1, "blocked": 2}
    rows.sort(key=lambda u: (order.get(u.status, 3), u.login))
    return {"users": [user_dict(db, u, counts) for u in rows], "signup": auth.signup_mode(),
            "max_apps": get_settings().user_max_apps, "max_servers": get_settings().user_max_servers}


class InviteBody(BaseModel):
    login: str = Field(min_length=1, max_length=39, pattern=r"^@?[A-Za-z0-9-]+$")
    role: str = Field(default="member", pattern="^(admin|member)$")


@users.post("", status_code=201)
def invite_user(body: InviteBody, request: Request, db: Session = Depends(get_db)):
    """Add (pre-approve) a GitHub login. They can sign in and deploy straight away, in any sign-up mode."""
    from ..services import owners

    login = body.login.lstrip("@")
    key = login.lower()
    actor = getattr(request.state, "user", None) or "admin"
    u = db.get(User, key)
    if u:
        u.status, u.role = "active", body.role
    else:
        u = User(login=key, display_login=login, role=body.role, status="active")
        db.add(u)
    u.decided_by = actor
    audit(db, actor, "user.invite", "user", key, f"Added @{login} as {body.role}")
    db.commit()
    return user_dict(db, u, owners.counts(db))


class UserPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|pending|blocked)$")
    role: str | None = Field(default=None, pattern="^(admin|member)$")
    note: str | None = Field(default=None, max_length=200)


@users.patch("/{login}")
def update_user(login: str, body: UserPatch, request: Request, db: Session = Depends(get_db)):
    """Approve, block or unblock an account, or make it an admin or a member."""
    from fastapi import HTTPException

    from ..services import owners

    u = db.get(User, login.lower())
    if not u:
        raise HTTPException(404, "user not found")
    actor = getattr(request.state, "user", None) or "admin"
    if auth.is_built_in_admin(db, u.login) and (body.status in ("blocked", "pending") or body.role == "member"):
        raise HTTPException(409, "This account is the server owner or listed in DEVNEXA_GITHUB_ADMINS, so it "
                                 "stays an active admin.")
    if actor.lower() == u.login and (body.status == "blocked" or body.role == "member"):
        raise HTTPException(409, "You cannot block or demote yourself.")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(u, k, v)
    u.decided_by = actor
    what = ", ".join(f"{k} {v}" for k, v in changes.items()) or "no change"
    audit(db, actor, "user.update", "user", u.login, f"@{u.display_login}: {what}", **changes)
    db.commit()
    return user_dict(db, u, owners.counts(db))
