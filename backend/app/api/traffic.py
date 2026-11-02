"""Live user traffic for deployed apps: requests, visitors, errors and the pages that fail."""

from fastapi import APIRouter, Query

from ..services.traffic import store

router = APIRouter(prefix="/api", tags=["traffic"])


@router.get("/traffic")
def traffic(minutes: int = Query(30, ge=1, le=60), app: str | None = None, errors_only: bool = False,
            path: str | None = None, limit: int = Query(50, ge=1, le=200)):
    """Per-minute requests and errors, per-app totals, failing pages and the latest requests.

    Client IPs are masked (last octet hidden) before they leave the server.
    """
    return store.snapshot(minutes=minutes, app=app or None, errors_only=errors_only, recent_limit=limit,
                          path=path or None)
