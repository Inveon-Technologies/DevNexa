"""Who owns what, so each signed-in user sees and changes only their own apps, servers and backups.

* An app name belongs to the user who first deployed it (`AppOwner`). Everything about the app follows it: its
  deployments, versions, issues, test URLs, data backups, databases and trash items.
* A test URL (`<app>-pr-N`, `<app>-fix-N`, `<app>-test-N`) belongs to the owner of its app.
* A server belongs to the user who added it (`Server.owner`).
* Apps and servers from before multi-user sign-in have no owner and belong to the admins.

A `Viewer` is who is asking: a signed-in login, plus whether they are an admin. Admins (and the admin key, and
local mode with no sign-in set up) see everything."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppDeployment, AppOwner, Server

PREVIEW_RE = re.compile(r"-(pr|test|fix)-\d+$")


class NameTaken(Exception):
    pass


class QuotaExceeded(Exception):
    pass


@dataclass(frozen=True)
class Viewer:
    login: str | None
    is_admin: bool

    @property
    def sees_all(self) -> bool:
        return self.is_admin


ADMIN = Viewer(None, True)
NOBODY = Viewer(None, False)


def _norm(login: str | None) -> str | None:
    return login.lower() if login else None


def app_owner(db: Session, app: str) -> str | None:
    row = db.get(AppOwner, app)
    if row:
        return row.owner
    # A test URL created before ownership existed: it belongs to whoever owns its app.
    d = db.scalars(select(AppDeployment).where(AppDeployment.app == app).order_by(AppDeployment.id.desc())).first()
    parent = ((d.options or {}).get("preview_of") if d else None)
    if parent and parent != app:
        return app_owner(db, parent)
    return None


def owner_for_new_deployment(db: Session, options: dict | None, actor: str | None) -> str | None:
    """Whose app a deployment that is being created belongs to."""
    opts = options or {}
    if opts.get("preview_of"):
        return app_owner(db, opts["preview_of"])
    return _norm(opts.get("owner") or opts.get("gh_user"))


def claim(db: Session, app: str, login: str | None) -> None:
    """Record that `login` owns `app` if nobody does yet. Raises NameTaken if somebody else does."""
    if not login:
        return
    login = login.lower()
    current = app_owner(db, app)
    if current and current != login:
        raise NameTaken(f"The app name '{app}' is already used by another account. Pick another name.")
    if not db.get(AppOwner, app):
        db.add(AppOwner(app=app, owner=login))
        db.flush()


def app_exists(db: Session, app: str) -> bool:
    return bool(db.scalars(select(AppDeployment.id).where(AppDeployment.app == app)).first())


def can_see_app(db: Session, viewer: Viewer, app: str) -> bool:
    if viewer.sees_all:
        return True
    return bool(viewer.login) and app_owner(db, app) == viewer.login.lower()


def visible_apps(db: Session, viewer: Viewer) -> set[str] | None:
    """The app names this viewer may see, test URLs included. None: all of them."""
    if viewer.sees_all:
        return None
    if not viewer.login:
        return set()
    login = viewer.login.lower()
    owned = {a for (a,) in db.execute(select(AppOwner.app).where(AppOwner.owner == login))}
    # test URLs of owned apps that have no row of their own yet
    for app, options in db.execute(select(AppDeployment.app, AppDeployment.options)):
        parent = (options or {}).get("preview_of")
        if parent in owned:
            owned.add(app)
    return owned


def apps_owned(db: Session, login: str) -> int:
    rows = db.execute(select(AppOwner.app).where(AppOwner.owner == login.lower())).all()
    return sum(1 for (a,) in rows if not PREVIEW_RE.search(a))


def check_app_quota(db: Session, viewer: Viewer, app: str) -> None:
    """Members may own up to DEVNEXA_USER_MAX_APPS apps (redeploying one they own is always fine)."""
    limit = get_settings().user_max_apps
    if viewer.is_admin or not viewer.login or not limit or app_owner(db, app):
        return
    if apps_owned(db, viewer.login) >= limit:
        raise QuotaExceeded(f"You already have {limit} apps, the most this server allows per account. "
                            "Delete one or ask an admin to raise DEVNEXA_USER_MAX_APPS.")


# ------------------------------------------------------------------ servers
def can_see_server(viewer: Viewer, server: Server) -> bool:
    if viewer.sees_all:
        return True
    return bool(viewer.login) and (server.owner or "") == viewer.login.lower()


def visible_servers(db: Session, viewer: Viewer) -> list[Server]:
    q = select(Server).order_by(Server.id)
    if not viewer.sees_all:
        if not viewer.login:
            return []
        q = q.where(Server.owner == viewer.login.lower())
    return list(db.scalars(q))


def check_server_quota(db: Session, viewer: Viewer) -> None:
    limit = get_settings().user_max_servers
    if viewer.is_admin or not viewer.login or not limit:
        return
    n = db.scalar(select(func.count()).select_from(Server).where(Server.owner == viewer.login.lower()))
    if n >= limit:
        raise QuotaExceeded(f"You already have {limit} servers, the most this server allows per account.")


# ------------------------------------------------------------- background work
def token_login(db: Session, app: str) -> str | None:
    """Whose GitHub access background work on `app` uses (fixes, test URLs, merges): its owner's. None for apps
    from before multi-user sign-in, which use the admins' shared connection."""
    return app_owner(db, app)


def counts(db: Session) -> dict[str, dict[str, int]]:
    """{login: {apps, servers}} for the Users page."""
    out: dict[str, dict[str, int]] = {}
    for app, owner in db.execute(select(AppOwner.app, AppOwner.owner)):
        if not PREVIEW_RE.search(app):
            out.setdefault(owner, {"apps": 0, "servers": 0})["apps"] += 1
    for (owner,) in db.execute(select(Server.owner).where(Server.owner.is_not(None))):
        out.setdefault(owner, {"apps": 0, "servers": 0})["servers"] += 1
    return out
