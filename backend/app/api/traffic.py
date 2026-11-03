"""Live user traffic for deployed apps: requests, visitors, errors and the pages that fail."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import owners
from ..services.traffic import store
from .apps import viewer

router = APIRouter(prefix="/api", tags=["traffic"])


@router.get("/traffic")
def traffic(request: Request, minutes: int = Query(30, ge=1, le=60), app: str | None = None,
            errors_only: bool = False, path: str | None = None, limit: int = Query(50, ge=1, le=200),
            db: Session = Depends(get_db)):
    """Per-minute requests and errors, per-app totals, failing pages and the latest requests.

    Client IPs are masked (last octet hidden) before they leave the server. Signed-in members see only their own
    apps' traffic and admins see all of it. The simulated demo traffic is shown to everyone.
    """
    snap = store.snapshot  # resolved here so tests can swap the store
    allowed = None
    if store.source != "simulated":
        allowed = owners.visible_apps(db, viewer(request, db))
    return snap(minutes=minutes, app=app or None, errors_only=errors_only, recent_limit=limit,
                path=path or None, allowed=allowed)
