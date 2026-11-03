"""Data backups, managed databases, the trash, and backup import/download for deployed apps."""

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..agents import deploy as deploy_agent
from ..agents import runner
from ..config import get_settings
from ..db import get_db
from ..models import AppDeployment
from ..services import backups
from .apps import admin, dep_dict, user

router = APIRouter(prefix="/api/live", tags=["backups and trash"])


def _app_or_404(db: Session, app: str) -> None:
    if not db.scalars(select(AppDeployment.id).where(AppDeployment.app == app)).first():
        raise HTTPException(404, "app not found")


def _actor(request: Request, given: str) -> str:
    return user(request) or given or "operator"


class ActorBody(BaseModel):
    actor: str = "operator"


# ------------------------------------------------------------------ backups
@router.get("/apps/{app}/data", dependencies=admin)
def data_overview(app: str, db: Session = Depends(get_db)):
    """What gets backed up, the smart backup policy, the backups themselves and the database."""
    _app_or_404(db, app)
    return backups.overview(db, app)


class BackupBody(BaseModel):
    actor: str = "operator"
    note: str | None = Field(default=None, max_length=500)


@router.post("/apps/{app}/data-backups", status_code=201, dependencies=admin)
def backup_now(app: str, body: BackupBody, request: Request, db: Session = Depends(get_db)):
    _app_or_404(db, app)
    db.close()
    try:
        bid, _ = backups.create_backup(app, "manual", _actor(request, body.actor), note=body.note)
    except backups.NothingToBackUp as e:
        raise HTTPException(400, str(e)) from e
    except backups.BackupError as e:
        raise HTTPException(502, f"Backup failed: {e}") from e
    with backups._session() as s:
        return backups.backup_dict(s.get(backups.AppBackup, bid))


class RestoreBody(BaseModel):
    actor: str = "operator"
    with_code: bool = False
    parts: list[str] = Field(default_factory=lambda: ["data", "db"])


@router.post("/apps/{app}/data-backups/{backup_id}/restore", dependencies=admin)
def restore(app: str, backup_id: int, body: RestoreBody, request: Request):
    try:
        return backups.restore(app, backup_id, _actor(request, body.actor), body.with_code,
                               tuple(p for p in body.parts if p in ("data", "db")))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


class PinBody(BaseModel):
    pinned: bool = True
    actor: str = "operator"


@router.post("/apps/{app}/data-backups/{backup_id}/pin", dependencies=admin)
def pin(app: str, backup_id: int, body: PinBody, db: Session = Depends(get_db)):
    b = db.get(backups.AppBackup, backup_id)
    if not b or b.app != app:
        raise HTTPException(404, "backup not found")
    b.pinned = body.pinned
    db.commit()
    return backups.backup_dict(b)


@router.delete("/apps/{app}/data-backups/{backup_id}", dependencies=admin)
def delete_backup(app: str, backup_id: int, request: Request, actor: str = "operator", db: Session = Depends(get_db)):
    b = db.get(backups.AppBackup, backup_id)
    if not b or b.app != app:
        raise HTTPException(404, "backup not found")
    try:
        b = backups.trash_backup(db, backup_id, _actor(request, actor))
    except KeyError as e:
        raise HTTPException(409, "This backup is already in the trash.") from e
    db.commit()
    return backups.backup_dict(b)


@router.get("/apps/{app}/data-backups/{backup_id}/download", dependencies=admin)
def download(app: str, backup_id: int, db: Session = Depends(get_db)):
    try:
        path, name = backups.export_bundle(db, app, backup_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(path, media_type="application/gzip", filename=name,
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


@router.post("/apps/{app}/data-backups/import", status_code=201, dependencies=admin)
async def import_backup(app: str, request: Request, filename: str = Query(min_length=1, max_length=200),
                        engine: str | None = Query(default=None, pattern="^(postgres|mysql|mongo)$"),
                        restore_now: bool = False, actor: str = "operator", db: Session = Depends(get_db)):
    """Upload a backup file as the raw request body (no multipart), streamed to disk."""
    _app_or_404(db, app)
    db.close()
    limit = get_settings().backup_import_max_mb * 1024 * 1024
    incoming = backups.root() / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=incoming, suffix="-" + Path(filename).name[-60:])
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"The file is larger than {limit // (1024 * 1024)} MB "
                                             "(DEVNEXA_BACKUP_IMPORT_MAX_MB).")
                f.write(chunk)
        if not size:
            raise HTTPException(400, "The upload is empty.")
        who = _actor(request, actor)
        try:
            bid = await run_in_threadpool(backups.import_backup, app, Path(tmp), filename, who, engine)
        except backups.ImportError_ as e:
            raise HTTPException(400, str(e)) from e
    finally:
        Path(tmp).unlink(missing_ok=True)
    result = None
    if restore_now:
        try:
            result = await run_in_threadpool(backups.restore, app, bid, who)
        except (KeyError, ValueError) as e:
            result = {"restored": [], "errors": [str(e)]}
    with backups._session() as s:
        return {**backups.backup_dict(s.get(backups.AppBackup, bid)), "restore": result}


# ---------------------------------------------------------------- databases
class DatabaseBody(BaseModel):
    engine: str = Field(default="postgres", pattern="^(postgres|mysql|mongo)$")
    restart_app: bool = True
    actor: str = "operator"


@router.post("/apps/{app}/databases", status_code=201, dependencies=admin)
def add_database(app: str, body: DatabaseBody, request: Request, db: Session = Depends(get_db)):
    _app_or_404(db, app)
    who = _actor(request, body.actor)
    try:
        d = backups.create_database(db, app, body.engine, who)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except backups.BackupError as e:
        db.commit()
        raise HTTPException(502, str(e)) from e
    dep = backups.restart_with_new_env(db, app, who) if body.restart_app else None
    db.commit()
    if dep:
        runner.submit(deploy_agent.run_deploy, dep.id)
    return {"database": backups.database_dict(d), "deployment": dep_dict(dep) if dep else None}


@router.delete("/apps/{app}/databases/{db_id}", dependencies=admin)
def delete_database(app: str, db_id: int, request: Request, actor: str = "operator", restart_app: bool = True,
                    db: Session = Depends(get_db)):
    d = db.get(backups.AppDatabase, db_id)
    if not d or d.app != app:
        raise HTTPException(404, "database not found")
    db.close()
    who = _actor(request, actor)
    try:
        res = backups.trash_database(db_id, who)
    except KeyError as e:
        raise HTTPException(409, "This database is not running (already in the trash?).") from e
    return {**res, "deployment": _restart(app, who) if restart_app else None}


def _restart(app: str, who: str):
    with backups._session() as s:
        dep = backups.restart_with_new_env(s, app, who)
        dep_id, out = (dep.id, dep_dict(dep)) if dep else (None, None)
    if dep_id:
        runner.submit(deploy_agent.run_deploy, dep_id)
    return out


# -------------------------------------------------------------------- trash
@router.get("/trash", dependencies=admin)
def list_trash(app: str | None = None, db: Session = Depends(get_db)):
    return backups.trash(db, app)


@router.post("/trash/{kind}/{item_id}/restore", dependencies=admin)
def restore_from_trash(kind: str, item_id: int, body: ActorBody, request: Request, db: Session = Depends(get_db)):
    who = _actor(request, body.actor)
    try:
        if kind == "backup":
            out = backups.backup_dict(backups.restore_backup_from_trash(db, item_id, who))
            db.commit()
            return out
        if kind == "database":
            d = backups.restore_database(db, item_id, who)
            db.commit()
            return {"database": backups.database_dict(d), "deployment": _restart(d.app, who)}
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except backups.BackupError as e:
        raise HTTPException(502, str(e)) from e
    raise HTTPException(404, "unknown trash item kind")


@router.delete("/trash/{kind}/{item_id}", dependencies=admin)
def purge(kind: str, item_id: int, request: Request, actor: str = "operator", db: Session = Depends(get_db)):
    who = _actor(request, actor)
    try:
        if kind == "backup":
            backups.purge_backup(db, item_id, who)
        elif kind == "database":
            backups.purge_database(db, item_id, who)
        else:
            raise HTTPException(404, "unknown trash item kind")
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    db.commit()
    return {"purged": item_id, "kind": kind}


@router.delete("/trash", dependencies=admin)
def empty(request: Request, app: str | None = None, actor: str = "operator", db: Session = Depends(get_db)):
    n = backups.empty_trash(db, _actor(request, actor), app)
    db.commit()
    return {"purged": n}
