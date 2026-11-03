"""Smart data backups, managed databases, the trash, and backup import/export for deployed apps.

What a backup holds is worked out per app ("smart"): the app's persistent `/data` volume, its database (one DevNexa
runs for it, or the one its DATABASE_URL / MONGODB_URI points to: PostgreSQL, MySQL/MariaDB or MongoDB), and a note
of which code version was live. Backups run on a schedule and automatically before risky changes (a new version
going live, a restore, deleting a database). A content fingerprint lets scheduled backups skip when nothing changed,
and tiered retention keeps the last N plus one per day, week and month. Retention never hard-deletes: pruned backups
go to the trash, which is emptied after `trash_days`.

Files live in `<backups_dir>/<app>/<id>/` (trash: `<backups_dir>/.trash/<app>/<id>/`). The backend reads and writes
them directly; short-lived helper containers (pg_dump, mysqldump, mongodump, tar) see the same folder at /backups
through `backups_volume`, so the data never passes through the backend's memory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import secrets
import shutil
import tarfile
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppBackup, AppDatabase, AppDeployment, AppRelease
from . import releases
from .audit import audit

log = logging.getLogger("devnexa.backups")

FORMAT = "devnexa-backup/1"
ENGINES = {
    "postgres": {"image": "postgres:16-alpine", "port": 5432, "title": "PostgreSQL"},
    "mysql": {"image": "mysql:8.4", "port": 3306, "title": "MySQL"},
    "mongo": {"image": "mongo:7", "port": 27017, "title": "MongoDB"},
}
DB_ENV_KEYS = ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "MYSQL_URL", "MONGODB_URI", "MONGODB_URL",
               "MONGO_URL", "MONGO_URI")
DB_FILES = {"postgres": "db.sql.gz", "mysql": "db.sql.gz", "mongo": "db.archive.gz"}
ROTATING = ("scheduled", "manual", "pre-deploy", "pre-restore")  # kinds that retention may prune
# Lines that differ between two dumps of identical data; ignored by the fingerprint.
VOLATILE = (b"\\restrict", b"\\unrestrict", b"-- Dump completed", b"-- Started on", b"-- Completed on")


class BackupError(Exception):
    pass


class NothingToBackUp(BackupError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def _ex():
    return releases._executor()


def _session():
    from ..db import session_scope

    return session_scope()


# --------------------------------------------------------------------- paths
def root() -> Path:
    p = Path(get_settings().backups_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def rel_dir(b: AppBackup) -> str:
    return f".trash/{b.app}/{b.id}" if b.status == "trashed" else f"{b.app}/{b.id}"


def local_dir(b: AppBackup) -> Path:
    return root() / rel_dir(b)


def _mount(read_only: bool = False) -> str:
    s = get_settings()
    src = s.backups_volume or str(Path(s.backups_dir).resolve())
    return f"{src}:/backups" + (":ro" if read_only else "")


def data_volume(app: str) -> str:
    return f"devnexa-data-{app}"


# ------------------------------------------------------------------ settings
def policy(db: Session, app: str) -> dict:
    st = releases.get_settings(db, app)
    return {**st["backup_policy"], "enabled": st["steps"]["smart_backup"], "persistent_data": st["steps"]["persistent_data"]}


def _save_policy_field(db: Session, app: str, **fields) -> None:
    cur = releases.get_settings(db, app)
    cur["backup_policy"] = {**cur["backup_policy"], **fields}
    releases.save_settings(db, app, cur)


# ------------------------------------------------------------- databases
def engine_of(url: str) -> str | None:
    scheme = urlsplit(url).scheme.lower().split("+")[0]
    return {"postgres": "postgres", "postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql",
            "mongodb": "mongo"}.get(scheme)


def _plain_url(url: str) -> str:
    """postgresql+psycopg://… -> postgresql://… (the CLI tools do not know SQLAlchemy driver suffixes)."""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+")[0] if not parts.scheme.startswith("mongodb+srv") else parts.scheme
    return parts._replace(scheme=scheme).geturl()


def active_database(db: Session, app: str) -> AppDatabase | None:
    return db.scalars(select(AppDatabase).where(AppDatabase.app == app, AppDatabase.status == "running")
                      .order_by(AppDatabase.id.desc())).first()


def internal_url(d: AppDatabase) -> str:
    port = ENGINES[d.engine]["port"]
    if d.engine == "postgres":
        return f"postgresql://{d.username}:{d.password}@{d.container}:{port}/{d.db_name}"
    if d.engine == "mysql":
        return f"mysql://{d.username}:{d.password}@{d.container}:{port}/{d.db_name}"
    return f"mongodb://{d.username}:{d.password}@{d.container}:{port}/{d.db_name}?authSource=admin"


def db_env(d: AppDatabase) -> dict:
    url = internal_url(d)
    env = {"DATABASE_URL": url}
    if d.engine == "mongo":
        env["MONGODB_URI"] = url
    return env


def database_dict(d: AppDatabase) -> dict:
    url = internal_url(d)
    return {"id": d.id, "app": d.app, "engine": d.engine, "engine_title": ENGINES[d.engine]["title"],
            "container": d.container, "volume": d.volume, "db_name": d.db_name, "username": d.username,
            "status": d.status, "url_masked": url.replace(d.password, "••••••"), "env_keys": sorted(db_env(d)),
            "created_by": d.created_by, "created_at": d.created_at.isoformat() if d.created_at else None,
            "trashed_at": d.trashed_at.isoformat() if d.trashed_at else None, "trashed_by": d.trashed_by,
            "expires_at": d.expires_at.isoformat() if d.expires_at else None}


def _db_run_cmd(d: AppDatabase) -> list[str]:
    s = get_settings()
    e = d.engine
    cmd = ["docker", "run", "-d", "--name", d.container, "--network", s.apps_network, "--restart", "unless-stopped",
           "--label", f"devnexa.db={d.app}", "--memory", "512m"]
    if e == "postgres":
        cmd += ["-e", f"POSTGRES_DB={d.db_name}", "-e", f"POSTGRES_USER={d.username}",
                "-e", f"POSTGRES_PASSWORD={d.password}", "-v", f"{d.volume}:/var/lib/postgresql/data"]
    elif e == "mysql":
        cmd += ["-e", f"MYSQL_DATABASE={d.db_name}", "-e", f"MYSQL_USER={d.username}",
                "-e", f"MYSQL_PASSWORD={d.password}", "-e", "MYSQL_RANDOM_ROOT_PASSWORD=yes",
                "-v", f"{d.volume}:/var/lib/mysql"]
    else:
        cmd += ["-e", f"MONGO_INITDB_ROOT_USERNAME={d.username}", "-e", f"MONGO_INITDB_ROOT_PASSWORD={d.password}",
                "-e", f"MONGO_INITDB_DATABASE={d.db_name}", "-v", f"{d.volume}:/data/db"]
    return cmd + [ENGINES[e]["image"]]


def create_database(db: Session, app: str, engine: str, actor: str) -> AppDatabase:
    if engine not in ENGINES:
        raise ValueError(f"Unknown database engine {engine!r}. Use one of: {', '.join(ENGINES)}.")
    if active_database(db, app):
        raise ValueError(f"{app} already has a database. Delete it first (it goes to the trash).")
    if remote_server(db, app):
        raise ValueError(f"{app} runs on a remote server. Managed databases run on the DevNexa host only for now.")
    name = app.replace("-", "_")[:40]
    d = AppDatabase(app=app, engine=engine, container="", volume="", db_name=name, username=name[:30] or "app",
                    password=secrets.token_hex(16), created_by=actor)
    db.add(d)
    db.flush()
    d.container, d.volume = f"devnexa-db-{app}-{d.id}", f"devnexa-dbdata-{app}-{d.id}"
    code, out = _ex().run(_db_run_cmd(d))
    if code != 0:
        d.status = "failed"
        audit(db, actor, "app.database.failed", "app_database", d.id, f"{app}: {out[-300:]}")
        raise BackupError(f"Could not start the {ENGINES[engine]['title']} container: {out[-300:]}")
    audit(db, actor, "app.database.create", "app_database", d.id,
          f"{app}: new {ENGINES[engine]['title']} database {d.container} (DATABASE_URL is set on the next start)")
    return d


def trash_database(db_id: int, actor: str) -> dict:
    """Delete a database the safe way: take a final backup, stop it, keep its volume in the trash."""
    with _session() as db:
        d = db.get(AppDatabase, db_id)
        if not d or d.status != "running":
            raise KeyError("database not found")
        app = d.app
    final = None
    try:
        final, _ = create_backup(app, "final", actor, note="Final backup taken before the database was deleted",
                                 only=("db",))
    except BackupError as e:
        log.warning("final backup of %s failed: %s", app, e)
    with _session() as db:
        d = db.get(AppDatabase, db_id)
        _ex().run(["docker", "rm", "-f", d.container])
        d.status, d.trashed_at, d.trashed_by = "trashed", now(), actor
        d.expires_at = now() + timedelta(days=get_settings().trash_days)
        audit(db, actor, "app.database.trash", "app_database", d.id,
              f"{app}: database moved to the trash (volume {d.volume} kept until {d.expires_at:%Y-%m-%d})")
        return {"database": database_dict(d), "final_backup_id": final}


def restore_database(db: Session, db_id: int, actor: str) -> AppDatabase:
    d = db.get(AppDatabase, db_id)
    if not d or d.status != "trashed":
        raise KeyError("database not in the trash")
    if active_database(db, d.app):
        raise ValueError(f"{d.app} already has a running database. Delete it first.")
    _ex().run(["docker", "rm", "-f", d.container])
    code, out = _ex().run(_db_run_cmd(d))
    if code != 0:
        raise BackupError(f"Could not start the database again: {out[-300:]}")
    d.status, d.trashed_at, d.trashed_by, d.expires_at = "running", None, None, None
    audit(db, actor, "app.database.restore", "app_database", d.id, f"{d.app}: database restored from the trash")
    return d


def purge_database(db: Session, db_id: int, actor: str) -> None:
    d = db.get(AppDatabase, db_id)
    if not d or d.status != "trashed":
        raise KeyError("database not in the trash")
    _ex().run(["docker", "rm", "-f", d.container])
    _ex().run(["docker", "volume", "rm", d.volume])
    d.status = "purged"
    audit(db, actor, "app.database.purge", "app_database", d.id, f"{d.app}: database {d.volume} permanently deleted")


# ------------------------------------------------------------- run-time wiring
def runtime(app: str) -> tuple[list[str], dict]:
    """Extra `docker run` arguments and env for an app container: its /data volume and its database URL."""
    with _session() as db:
        st = releases.get_settings(db, app)
        d = active_database(db, app)
        args, env = [], {}
        if st["steps"]["persistent_data"]:
            args += ["-v", f"{data_volume(app)}:/data"]
            env["DATA_DIR"] = "/data"
        if d and not remote_server(db, app):
            env.update(db_env(d))
    return args, env


def restart_with_new_env(db: Session, app: str, actor: str) -> AppDeployment | None:
    """Start the live version again (same image, no rebuild) so it picks up a new DATABASE_URL. The old container
    keeps serving until the new one passes its health check."""
    from ..agents import deploy

    live = releases.live_deployment(db, app)
    if not live or not live.image:
        return None
    opts = {k: v for k, v in (live.options or {}).items() if k not in ("reuse_image", "source")}
    opts.update(reuse_image=live.image, port=live.port, build_kind=live.build_kind, source="restart")
    return deploy.create_deployment(db, live.repo, live.branch, app, live.root_dir, dict(live.env or {}), opts,
                                    actor=actor)


# --------------------------------------------------------------- sources
def remote_server(db: Session, app: str) -> int | None:
    """The remote server an app runs on (None: the DevNexa host). Data backups and managed databases are
    DevNexa-host only for now: backup files and database containers live next to the backend."""
    from . import servers

    return servers.server_of_app(db, app)


def sources(db: Session, app: str) -> list[dict]:
    """What a backup of this app contains, worked out from how the app runs."""
    out = []
    if remote_server(db, app):
        return out
    pol = policy(db, app)
    if pol["persistent_data"]:
        out.append({"type": "data", "volume": data_volume(app), "source": f"/data volume ({data_volume(app)})"})
    d = active_database(db, app)
    if d:
        out.append({"type": "db", "engine": d.engine, "url": internal_url(d),
                    "source": f"DevNexa {ENGINES[d.engine]['title']} database"})
    else:
        live = releases.live_deployment(db, app)
        env = dict((live.env if live else None) or {})
        for k in DB_ENV_KEYS:
            if env.get(k) and engine_of(env[k]):
                e = engine_of(env[k])
                out.append({"type": "db", "engine": e, "url": env[k], "source": f"{ENGINES[e]['title']} at ${k}"})
                break
    return out


def public_sources(db: Session, app: str) -> list[dict]:
    return [{k: v for k, v in s.items() if k != "url"} for s in sources(db, app)]


# ---------------------------------------------------------------- dumping
def _helper(args: list[str], script: str, image: str | None = None, network: bool = False,
            read_only: bool = False) -> tuple[int, str]:
    s = get_settings()
    cmd = ["docker", "run", "--rm", "-v", _mount(read_only)]
    if network:
        cmd += ["--network", s.apps_network]
    return _ex().run(cmd + args + [image or s.backup_helper_image, "sh", "-c", script], timeout=3600)


def _dump(src: dict, rel: str) -> dict:
    if src["type"] == "data":
        file = "data.tar.gz"
        code, out = _helper(["-v", f"{src['volume']}:/data:ro", "-e", f"OUT=/backups/{rel}/{file}"],
                            'tar czf "$OUT" -C /data .')
    else:
        engine, url = src["engine"], _plain_url(src["url"])
        file = DB_FILES[engine]
        env = ["-e", f"OUT=/backups/{rel}/{file}"]
        if engine == "postgres":
            script = ('set -o pipefail; pg_dump --clean --if-exists --no-owner --no-privileges "$DB_URL" '
                      '| gzip > "$OUT"')
            env += ["-e", f"DB_URL={url}"]
        elif engine == "mysql":
            env += _mysql_env(url)
            script = ('set -o pipefail; mysqldump -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" --single-transaction '
                      '--routines --triggers --skip-dump-date --no-tablespaces "$DB_NAME" | gzip > "$OUT"')
        else:
            env += ["-e", f"DB_URL={url}"]
            script = 'mongodump --uri="$DB_URL" --archive="$OUT" --gzip'
        code, out = _helper(env, script, ENGINES[engine]["image"], network=True)
    path = root() / rel / file
    if code != 0 or not path.is_file():
        tail = (out or "").strip().splitlines()[-3:]
        raise BackupError(" ".join(tail) or "the helper container wrote no file")
    part = {"type": src["type"], "file": file, "bytes": path.stat().st_size, "source": src["source"]}
    if src["type"] == "db":
        part["engine"] = src["engine"]
    return part


def _mysql_env(url: str) -> list[str]:
    u = urlsplit(url)
    return ["-e", f"DB_HOST={u.hostname or ''}", "-e", f"DB_PORT={u.port or 3306}",
            "-e", f"DB_USER={unquote(u.username or 'root')}", "-e", f"MYSQL_PWD={unquote(u.password or '')}",
            "-e", f"DB_NAME={u.path.lstrip('/').split('?')[0]}"]


def fingerprint(folder: Path, parts: list[dict]) -> str:
    """Hash of the backed-up content (not of the archive bytes, which change with timestamps)."""
    h = hashlib.sha256()
    for p in sorted(parts, key=lambda x: x["type"]):
        h.update(p["type"].encode() + b"\0")
        path = folder / p["file"]
        if p["file"].endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as tf:
                for m in sorted(tf.getmembers(), key=lambda m: m.name):
                    h.update(m.name.encode() + b"\0" + str(m.size).encode() + b"\0")
                    if m.isfile():
                        f = tf.extractfile(m)
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
        elif p["file"].endswith(".sql.gz"):
            with gzip.open(path, "rb") as f:
                for line in f:
                    if not line.startswith(VOLATILE):
                        h.update(line)
        else:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()


def verify(path: Path) -> bool:
    """Read the whole archive back; a truncated or corrupt file fails here, not on the day you need it."""
    try:
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as tf:
                for m in tf:
                    if m.isfile():
                        f = tf.extractfile(m)
                        while f.read(1 << 20):
                            pass
        elif path.name.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                while f.read(1 << 20):
                    pass
        return path.stat().st_size > 0
    except (OSError, tarfile.TarError, EOFError):
        return False


def _write_manifest(b: AppBackup, folder: Path) -> None:
    (folder / "manifest.json").write_text(json.dumps({
        "format": FORMAT, "app": b.app, "id": b.id, "kind": b.kind, "label": b.label,
        "created_at": b.created_at.isoformat() if b.created_at else now().isoformat(), "parts": b.parts,
        "release_label": b.release_label, "release_image": b.release_image, "commit_sha": b.commit_sha,
        "fingerprint": b.fingerprint, "note": b.note}, indent=2))


# ------------------------------------------------------------------ backup
KIND_LABEL = {"scheduled": "Scheduled backup", "manual": "Backup", "pre-deploy": "Before new version",
              "pre-restore": "Before restore", "final": "Final backup", "imported": "Imported"}


def create_backup(app: str, kind: str, actor: str, note: str | None = None,
                  only: tuple[str, ...] | None = None) -> tuple[int, bool]:
    """Back up the app's data now. Returns (backup id, skipped): a scheduled or pre-deploy backup whose content is
    identical to the last good backup is not kept, and the id of that earlier backup is returned instead."""
    if not _ex().available():
        raise BackupError("Docker is not available to the DevNexa backend.")
    with _session() as db:
        srcs = [s for s in sources(db, app) if not only or s["type"] in only]
        if not srcs:
            raise NothingToBackUp(f"{app} has no persistent data or database to back up.")
        pol = policy(db, app)
        rel = db.scalars(select(AppRelease).where(AppRelease.app == app, AppRelease.status == "live")).first()
        live = releases.live_deployment(db, app)
        b = AppBackup(app=app, kind=kind, status="running", created_by=actor, note=note,
                      label=f"{KIND_LABEL.get(kind, 'Backup')} {now():%Y-%m-%d %H:%M}",
                      pinned=kind in ("final", "imported"), release_label=rel.label if rel else None,
                      release_image=rel.image if rel else None,
                      commit_sha=(rel.commit_sha if rel else None) or (live.commit_sha if live else None))
        db.add(b)
        db.flush()
        bid, rd = b.id, rel_dir(b)
    folder = root() / rd
    folder.mkdir(parents=True, exist_ok=True)
    parts, errors = [], []
    for src in srcs:
        try:
            parts.append(_dump(src, rd))
        except BackupError as e:
            errors.append(f"{src['source']}: {e}")
    fp = fingerprint(folder, parts) if parts else None
    ok = all(verify(folder / p["file"]) for p in parts)
    with _session() as db:
        b = db.get(AppBackup, bid)
        b.parts, b.fingerprint, b.verified = parts, fp, bool(parts) and ok
        b.size_bytes = sum(p["bytes"] for p in parts)
        if errors:
            b.note = "; ".join(filter(None, [note, *errors]))[:2000]
        prev = db.scalars(select(AppBackup).where(AppBackup.app == app, AppBackup.status == "ready",
                                                  AppBackup.id != bid).order_by(AppBackup.id.desc())).first()
        if kind == "scheduled":
            _save_policy_field(db, app, last_run=now().isoformat())
        if not parts:
            b.status = "failed"
            audit(db, actor, "app.data_backup.failed", "app_backup", bid, f"{app}: {b.note}")
            shutil.rmtree(folder, ignore_errors=True)
            raise BackupError("; ".join(errors))
        if (kind in ("scheduled", "pre-deploy") and pol.get("skip_unchanged", True) and prev and not errors
                and prev.fingerprint == fp and {p["type"] for p in prev.parts} == {p["type"] for p in parts}):
            db.delete(b)
            _save_policy_field(db, app, last_skipped=now().isoformat())
            audit(db, actor, "app.data_backup.unchanged", "app_backup", prev.id,
                  f"{app}: nothing changed since {prev.label}, no new backup kept")
            prev_id = prev.id
            skipped = True
        else:
            b.status = "ready" if ok else "failed"
            _write_manifest(b, folder)
            audit(db, actor, "app.data_backup", "app_backup", bid,
                  f"{app}: {b.label} ({', '.join(p['type'] for p in parts)}, {_human(b.size_bytes)}"
                  f"{', verified' if ok else ', VERIFY FAILED'})")
            skipped = False
    if skipped:
        shutil.rmtree(folder, ignore_errors=True)
        return prev_id, True
    if kind in ROTATING:
        apply_retention(app)
    return bid, False


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# --------------------------------------------------------------- retention
def plan_retention(items: list[tuple[int, datetime]], keep_last: int, keep_daily: int, keep_weekly: int,
                   keep_monthly: int) -> set[int]:
    """Tiered (grandfather-father-son) retention. `items` is newest first; returns the ids to keep: the newest
    `keep_last`, plus the newest backup of each of the last `keep_daily` days, `keep_weekly` weeks and
    `keep_monthly` months that have backups."""
    keep = {i for i, _ in items[:keep_last]}
    for limit, bucket in ((keep_daily, lambda t: t.date()), (keep_weekly, lambda t: t.isocalendar()[:2]),
                          (keep_monthly, lambda t: (t.year, t.month))):
        seen = set()
        for i, t in items:
            k = bucket(t)
            if k in seen:
                continue
            if len(seen) >= limit:
                break
            seen.add(k)
            keep.add(i)
    return keep


def apply_retention(app: str, actor: str = "backup-agent") -> list[int]:
    with _session() as db:
        pol = policy(db, app)
        rows = list(db.scalars(select(AppBackup).where(
            AppBackup.app == app, AppBackup.status == "ready", AppBackup.pinned.is_(False),
            AppBackup.kind.in_(ROTATING)).order_by(AppBackup.created_at.desc(), AppBackup.id.desc())))
        keep = plan_retention([(r.id, r.created_at) for r in rows], pol["keep_last"], pol["keep_daily"],
                              pol["keep_weekly"], pol["keep_monthly"])
        pruned = []
        for r in rows:
            if r.id not in keep:
                _to_trash(db, r, actor, "retention")
                pruned.append(r.id)
        return pruned


# -------------------------------------------------------------------- trash
def _to_trash(db: Session, b: AppBackup, actor: str, reason: str) -> None:
    src = local_dir(b)
    b.status = "trashed"
    dst = local_dir(b)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.rmtree(dst, ignore_errors=True)
        shutil.move(str(src), str(dst))
    b.trashed_at, b.trashed_by, b.trash_reason = now(), actor, reason
    b.expires_at = now() + timedelta(days=get_settings().trash_days)
    audit(db, actor, "app.data_backup.trash", "app_backup", b.id, f"{b.app}: {b.label} moved to the trash ({reason})")


def trash_backup(db: Session, backup_id: int, actor: str) -> AppBackup:
    b = db.get(AppBackup, backup_id)
    if not b or b.status not in ("ready", "failed"):
        raise KeyError("backup not found")
    _to_trash(db, b, actor, "deleted")
    return b


def restore_backup_from_trash(db: Session, backup_id: int, actor: str) -> AppBackup:
    b = db.get(AppBackup, backup_id)
    if not b or b.status != "trashed":
        raise KeyError("backup not in the trash")
    src = local_dir(b)
    b.status = "ready" if b.parts else "failed"
    dst = local_dir(b)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.move(str(src), str(dst))
    if b.trash_reason == "retention":
        b.pinned = True  # otherwise the next retention pass would trash it again
    b.trashed_at = b.trashed_by = b.trash_reason = b.expires_at = None
    audit(db, actor, "app.data_backup.untrash", "app_backup", b.id, f"{b.app}: {b.label} restored from the trash")
    return b


def purge_backup(db: Session, backup_id: int, actor: str) -> None:
    b = db.get(AppBackup, backup_id)
    if not b or b.status != "trashed":
        raise KeyError("backup not in the trash")
    shutil.rmtree(local_dir(b), ignore_errors=True)
    b.status = "purged"
    audit(db, actor, "app.data_backup.purge", "app_backup", b.id, f"{b.app}: {b.label} permanently deleted")


def trash(db: Session, app: str | None = None) -> dict:
    qb = select(AppBackup).where(AppBackup.status == "trashed").order_by(AppBackup.trashed_at.desc())
    qd = select(AppDatabase).where(AppDatabase.status == "trashed").order_by(AppDatabase.trashed_at.desc())
    if app:
        qb, qd = qb.where(AppBackup.app == app), qd.where(AppDatabase.app == app)
    return {"backups": [backup_dict(b) for b in db.scalars(qb)],
            "databases": [database_dict(d) for d in db.scalars(qd)], "days": get_settings().trash_days}


def empty_trash(db: Session, actor: str, app: str | None = None) -> int:
    t = trash(db, app)
    for b in t["backups"]:
        purge_backup(db, b["id"], actor)
    for d in t["databases"]:
        purge_database(db, d["id"], actor)
    return len(t["backups"]) + len(t["databases"])


def purge_expired(at: datetime | None = None) -> int:
    at = at or now()
    n = 0
    with _session() as db:
        for b in db.scalars(select(AppBackup).where(AppBackup.status == "trashed", AppBackup.expires_at <= at)):
            purge_backup(db, b.id, "trash-expiry")
            n += 1
        for d in db.scalars(select(AppDatabase).where(AppDatabase.status == "trashed", AppDatabase.expires_at <= at)):
            purge_database(db, d.id, "trash-expiry")
            n += 1
    return n


# ------------------------------------------------------------------ restore
def restore(app: str, backup_id: int, actor: str, with_code: bool = False,
            types: tuple[str, ...] = ("data", "db")) -> dict:
    """Put a backup's data back. A safety backup of the current state is taken first, so a restore can be undone."""
    with _session() as db:
        b = db.get(AppBackup, backup_id)
        if not b or b.app != app:
            raise KeyError("backup not found")
        if b.status != "ready":
            raise ValueError("Only a ready backup can be restored" + (" (restore it from the trash first)."
                                                                        if b.status == "trashed" else "."))
        parts = [p for p in b.parts if p["type"] in types]
        if not parts:
            raise ValueError("This backup has none of the requested parts.")
        srcs = {s["type"]: s for s in sources(db, app)}
        pol = policy(db, app)
        rd, label, image = rel_dir(b), b.label, b.release_image
        for p in parts:
            if p["type"] == "data" and "data" not in srcs:
                raise ValueError("Persistent /data is switched off for this app; switch it on to restore files.")
            if p["type"] == "db":
                if "db" not in srcs:
                    raise ValueError("This app has no database to restore into. Add one first.")
                if srcs["db"]["engine"] != p.get("engine"):
                    raise ValueError(f"The backup holds a {p.get('engine')} dump but the app's database is "
                                     f"{srcs['db']['engine']}.")
        live = releases.live_deployment(db, app)
        containers = ([live.container] if live and live.container else []) + releases.lb_status(db, app)["replicas"]
    safety = None
    if pol.get("before_changes", True):
        try:
            safety, _ = create_backup(app, "pre-restore", actor, note=f"Taken automatically before restoring {label}")
        except BackupError as e:
            log.warning("safety backup before restore failed: %s", e)
    done, errors = [], []
    for p in parts:
        code, out = _restore_part(p, rd, srcs)
        if code != 0:
            errors.append(f"{p['type']}: {' '.join(out.strip().splitlines()[-3:])}")
        else:
            done.append(p["type"])
            if p["type"] == "db" and "ERROR:" in out:
                errors.append("db: restored with some SQL errors: " + next(
                    ln for ln in out.splitlines() if "ERROR:" in ln)[:200])
    if "data" in done:
        for c in containers:  # the app re-reads its files on start
            _ex().run(["docker", "restart", c])
    dep_id = None
    with _session() as db:
        if with_code and image:
            rel = db.scalars(select(AppRelease).where(AppRelease.app == app, AppRelease.image == image,
                                                      AppRelease.status != "removed")).first()
            if rel and rel.status != "live":
                dep_id = releases.rollback(db, app, rel.id, actor).id
            elif not rel:
                errors.append(f"code: version {image} is no longer kept, so only the data was restored")
        audit(db, actor, "app.data_backup.restore", "app_backup", backup_id,
              f"{app}: restored {', '.join(done) or 'nothing'} from {label}"
              + (f" (safety backup #{safety})" if safety else "") + (f"; errors: {'; '.join(errors)}" if errors else ""))
    if dep_id:
        from ..agents import deploy, runner

        runner.submit(deploy.run_deploy, dep_id)
    return {"restored": done, "errors": errors, "safety_backup_id": safety, "deployment_id": dep_id}


def _restore_part(p: dict, rd: str, srcs: dict) -> tuple[int, str]:
    env = ["-e", f"IN=/backups/{rd}/{p['file']}"]
    if p["type"] == "data":
        return _helper(env + ["-v", f"{srcs['data']['volume']}:/data"],
                       'find /data -mindepth 1 -delete && tar xzf "$IN" -C /data', read_only=True)
    engine, url = p["engine"], _plain_url(srcs["db"]["url"])
    if engine == "postgres":
        return _helper(env + ["-e", f"DB_URL={url}"],
                       'set -o pipefail; gunzip -c "$IN" | psql -q -v ON_ERROR_STOP=0 "$DB_URL" 2>&1',
                       ENGINES[engine]["image"], network=True, read_only=True)
    if engine == "mysql":
        return _helper(env + _mysql_env(url),
                       'set -o pipefail; gunzip -c "$IN" | mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME"',
                       ENGINES[engine]["image"], network=True, read_only=True)
    return _helper(env + ["-e", f"DB_URL={url}"], 'mongorestore --uri="$DB_URL" --drop --archive="$IN" --gzip',
                   ENGINES[engine]["image"], network=True, read_only=True)


# ------------------------------------------------------------------- import
class ImportError_(ValueError):
    pass


def _safe_name(name: str) -> str | None:
    while name.startswith("./"):
        name = name[2:]
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts:
        return None
    return str(p)


def _repack_tar(src: tarfile.TarFile, dst: Path, limit: int) -> None:
    """Copy only regular files and folders with safe relative paths: no links, devices or ../ escapes."""
    total = 0
    with tarfile.open(dst, "w:gz") as out:
        for m in src:
            name = _safe_name(m.name)
            if not name or name == "." or not (m.isfile() or m.isdir()):
                continue
            total += m.size
            if total > limit:
                raise ImportError_("The archive unpacks to more than the import limit.")
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.type = m.size, m.mtime, m.type
            info.mode = m.mode & 0o777
            out.addfile(info, src.extractfile(m) if m.isfile() else None)


def _gzip_to(src: Path, dst: Path) -> None:
    with src.open("rb") as f, gzip.open(dst, "wb") as g:
        shutil.copyfileobj(f, g, 1 << 20)


def _sniff_sql(head: bytes) -> str:
    t = head.decode("utf-8", "replace")
    if "mysqldump" in t or "ENGINE=" in t or "/*!40101" in t or "MariaDB dump" in t:
        return "mysql"
    return "postgres"


def import_backup(app: str, upload: Path, filename: str, actor: str, engine: str | None = None) -> int:
    """Turn an uploaded file into a backup: a DevNexa backup bundle, a database dump (.sql, .sql.gz, mongodump
    .archive), a folder archive (.tar.gz, .tgz, .tar, .zip) or a SQLite file (.sqlite, .db) for /data."""
    s = get_settings()
    limit = s.backup_import_max_mb * 1024 * 1024 * 10
    name = Path(filename).name.lower()
    with _session() as db:
        b = AppBackup(app=app, kind="imported", status="running", created_by=actor, pinned=True,
                      label=f"Imported {Path(filename).name[:80]}", note=f"Uploaded file {Path(filename).name}")
        db.add(b)
        db.flush()
        bid, rd = b.id, rel_dir(b)
        app_db = active_database(db, app)
        app_engine = app_db.engine if app_db else next((x["engine"] for x in sources(db, app) if x["type"] == "db"),
                                                       None)
    folder = root() / rd
    folder.mkdir(parents=True, exist_ok=True)
    parts: list[dict] = []
    meta: dict = {}
    try:
        if name.endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(upload, "r:*") as tf:
                names = {_safe_name(m.name): m for m in tf.getmembers()}
                bundle = "manifest.json" in names and names["manifest.json"].isfile()
                if bundle:  # a DevNexa backup (from Download), from this server or another one
                    meta = json.load(tf.extractfile(names["manifest.json"]))
                    if meta.get("format") != FORMAT:
                        raise ImportError_("manifest.json is not a DevNexa backup manifest.")
                    for p in meta.get("parts") or []:
                        m = names.get(p.get("file"))
                        if p.get("file") in ("data.tar.gz", *DB_FILES.values()) and m is not None and m.isfile() \
                                and p.get("type") in ("data", "db") and (p["type"] == "data" or p.get("engine") in ENGINES):
                            with (folder / p["file"]).open("wb") as f:
                                shutil.copyfileobj(tf.extractfile(m), f, 1 << 20)
                            parts.append({k: p[k] for k in ("type", "file", "engine") if k in p}
                                         | {"source": f"imported from {meta.get('app', '?')} #{meta.get('id', '?')}"})
            if not bundle:
                with tarfile.open(upload, "r:*") as tf:
                    _repack_tar(tf, folder / "data.tar.gz", limit)
                parts.append({"type": "data", "file": "data.tar.gz", "source": "imported folder archive"})
        elif name.endswith(".zip"):
            with zipfile.ZipFile(upload) as zf, tarfile.open(folder / "data.tar.gz", "w:gz") as out:
                total = 0
                for zi in zf.infolist():
                    n = _safe_name(zi.filename.rstrip("/"))
                    if not n or zi.is_dir():
                        continue
                    total += zi.file_size
                    if total > limit:
                        raise ImportError_("The archive unpacks to more than the import limit.")
                    info = tarfile.TarInfo(n)
                    info.size, info.mode = zi.file_size, 0o644
                    with zf.open(zi) as f:
                        out.addfile(info, f)
            parts.append({"type": "data", "file": "data.tar.gz", "source": "imported .zip"})
        elif name.endswith((".sql", ".sql.gz")):
            gz = name.endswith(".gz")
            with (gzip.open(upload, "rb") if gz else upload.open("rb")) as f:
                head = f.read(65536)
            e = engine or (app_engine if app_engine in ("postgres", "mysql") else None) or _sniff_sql(head)
            if e not in ("postgres", "mysql"):
                raise ImportError_("A .sql file is a PostgreSQL or MySQL dump.")
            if gz:
                shutil.copyfile(upload, folder / "db.sql.gz")
            else:
                _gzip_to(upload, folder / "db.sql.gz")
            parts.append({"type": "db", "engine": e, "file": "db.sql.gz", "source": f"imported {e} dump"})
        elif name.endswith((".archive", ".archive.gz", ".agz")):
            if name.endswith(".archive"):
                _gzip_to(upload, folder / "db.archive.gz")
            else:
                shutil.copyfile(upload, folder / "db.archive.gz")
            parts.append({"type": "db", "engine": "mongo", "file": "db.archive.gz", "source": "imported mongodump"})
        elif name.endswith((".sqlite", ".sqlite3", ".db")):
            with tarfile.open(folder / "data.tar.gz", "w:gz") as out:
                out.add(upload, arcname=Path(filename).name)
            parts.append({"type": "data", "file": "data.tar.gz", "source": "imported SQLite file"})
        else:
            raise ImportError_("Unsupported file. Upload a DevNexa backup (.tar.gz), a database dump (.sql, .sql.gz, "
                               ".archive), a folder archive (.tar.gz, .tgz, .zip) or a SQLite file (.sqlite, .db).")
        if not parts:
            raise ImportError_("The file holds nothing DevNexa can restore.")
        for p in parts:
            p["bytes"] = (folder / p["file"]).stat().st_size
            if not verify(folder / p["file"]):
                raise ImportError_(f"{p['file']} in the upload is damaged (it could not be read back).")
        fp = fingerprint(folder, parts)
    except (ImportError_, tarfile.TarError, zipfile.BadZipFile, OSError, EOFError, json.JSONDecodeError) as e:
        shutil.rmtree(folder, ignore_errors=True)
        with _session() as db:
            db.delete(db.get(AppBackup, bid))
        raise ImportError_(str(e) if isinstance(e, ImportError_) else f"Could not read the file: {e}") from e
    with _session() as db:
        b = db.get(AppBackup, bid)
        b.parts, b.fingerprint, b.verified, b.status = parts, fp, True, "ready"
        b.size_bytes = sum(p["bytes"] for p in parts)
        b.release_label, b.commit_sha = meta.get("release_label"), meta.get("commit_sha")
        b.release_image = meta.get("release_image") if meta.get("app") == app else None
        _write_manifest(b, folder)
        audit(db, actor, "app.data_backup.import", "app_backup", bid,
              f"{app}: imported {Path(filename).name} ({', '.join(p['type'] for p in parts)}, {_human(b.size_bytes)})")
    return bid


def export_bundle(db: Session, app: str, backup_id: int) -> tuple[Path, str]:
    """A single .tar.gz (manifest + parts) that can be downloaded and imported again, here or on another server."""
    b = db.get(AppBackup, backup_id)
    if not b or b.app != app or b.status not in ("ready", "trashed"):
        raise KeyError("backup not found")
    folder = local_dir(b)
    fd, tmp = tempfile.mkstemp(prefix="devnexa-export-", suffix=".tar.gz")
    os.close(fd)
    with tarfile.open(tmp, "w:gz") as out:
        for f in ["manifest.json", *[p["file"] for p in b.parts]]:
            if (folder / f).is_file():
                out.add(folder / f, arcname=f)
    return Path(tmp), f"{app}-backup-{b.id}-{b.created_at:%Y%m%d-%H%M}.tar.gz"


# ----------------------------------------------------------------- listing
def backup_dict(b: AppBackup) -> dict:
    return {"id": b.id, "app": b.app, "kind": b.kind, "status": b.status, "label": b.label, "parts": b.parts or [],
            "size_bytes": b.size_bytes, "size": _human(b.size_bytes or 0), "verified": b.verified,
            "pinned": b.pinned, "release_label": b.release_label, "commit_sha": b.commit_sha, "note": b.note,
            "created_by": b.created_by, "created_at": b.created_at.isoformat() if b.created_at else None,
            "trashed_at": b.trashed_at.isoformat() if b.trashed_at else None, "trashed_by": b.trashed_by,
            "trash_reason": b.trash_reason, "expires_at": b.expires_at.isoformat() if b.expires_at else None}


def overview(db: Session, app: str) -> dict:
    rows = db.scalars(select(AppBackup).where(AppBackup.app == app, AppBackup.status.in_(("ready", "failed",
                                                                                          "running")))
                      .order_by(AppBackup.id.desc()).limit(200)).all()
    d = active_database(db, app)
    pol = policy(db, app)
    last = pol.get("last_run")
    next_run = (datetime.fromisoformat(last) + timedelta(hours=pol["every_hours"])).isoformat() if last else None
    t = trash(db, app)
    return {"policy": pol, "next_run": next_run, "sources": public_sources(db, app),
            "remote_server": remote_server(db, app),
            "database": database_dict(d) if d else None, "engines": [{"key": k, "title": v["title"]}
                                                                     for k, v in ENGINES.items()],
            "backups": [backup_dict(b) for b in rows],
            "total_bytes": sum(b.size_bytes or 0 for b in rows if b.status == "ready"),
            "trash_count": len(t["backups"]) + len(t["databases"]), "trash_days": t["days"],
            "import_max_mb": get_settings().backup_import_max_mb}


# --------------------------------------------------------------- schedule
def due(db: Session, app: str, at: datetime | None = None) -> bool:
    pol = policy(db, app)
    if not pol["enabled"]:
        return False
    last = pol.get("last_run")
    return not last or (at or now()) - datetime.fromisoformat(last) >= timedelta(hours=pol["every_hours"])


def run_due(at: datetime | None = None) -> list[str]:
    """Called on every monitoring pass: back up the apps whose turn it is, and empty expired trash items."""
    with _session() as db:
        apps = sorted({a for (a,) in db.execute(select(AppDeployment.app).where(AppDeployment.status == "running"))
                       if not releases.is_preview(releases.live_deployment(db, a))})
        todo = [a for a in apps if due(db, a, at) and sources(db, a)]
    done = []
    for app in todo:
        try:
            create_backup(app, "scheduled", "backup-schedule")
            done.append(app)
        except BackupError as e:
            log.warning("scheduled backup of %s failed: %s", app, e)
            with _session() as db:  # do not retry on every 15 s tick
                _save_policy_field(db, app, last_run=now().isoformat(), last_error=str(e)[:300])
    purge_expired(at)
    return done


def before_deploy(app: str, out=None) -> int | None:
    """Back up data right before a new version starts (migrations may change the database)."""
    with _session() as db:
        pol = policy(db, app)
        if not (pol["enabled"] and pol.get("before_changes", True)) or not releases.live_deployment(db, app) \
                or not sources(db, app):
            return None
    try:
        bid, skipped = create_backup(app, "pre-deploy", "deploy-agent", note="Taken before a new version went live")
    except BackupError as e:
        if out:
            out(f"[backup] could not back up data before this version starts: {e}")
        return None
    if out:
        out(f"[backup] data unchanged since backup #{bid}, nothing new to keep" if skipped
            else f"[backup] data backed up as #{bid} before this version starts")
    return bid
