"""Sign in with GitHub (OAuth web flow) and the current session."""

import secrets
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..services import auth, github
from ..services.audit import audit

router = APIRouter(prefix="/api/auth", tags=["sign in"])


def _safe_next(path: str | None) -> str:
    return path if path and path.startswith("/") and not path.startswith("//") else "/deploy"


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    login = auth.current_user(request, db)
    out = {"signed_in": bool(login), "login": login, "github_login": auth.enabled(),
           "admin_key": bool(s.admin_token)}
    if login:
        conn = github.connection(db, login)
        out.update(name=conn.get("name"), avatar_url=conn.get("avatar_url"))
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
        login = github.profile(token)[0]["login"]
        if auth.is_allowed(db, login):
            github.save_user_token(db, token)
    except (auth.AuthError, github.GitHubError) as e:
        db.rollback()
        return _back(nxt, f"GitHub sign-in failed: {e}")
    if not auth.is_allowed(db, login, claim=False):
        audit(db, login, "auth.refused", "github", None, f"@{login} is not allowed to sign in")
        db.commit()
        return _back(nxt, f"@{login} is not allowed to use this DevNexa server. Ask the owner to add you to "
                          "DEVNEXA_GITHUB_ALLOWED_USERS.")
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
