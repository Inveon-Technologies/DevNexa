from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from .config import get_settings

settings = get_settings()

_engine_kwargs: dict = {"pool_pre_ping": True}
if settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **_engine_kwargs)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def strip_nul(value):
    """PostgreSQL text and JSON can't hold NUL (0x00) bytes, but build logs, container output and files read from
    a repo sometimes contain them. Drop them (recursively in JSON values) so one stray byte can't fail a save."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    return value


@event.listens_for(Session, "before_flush")
def _strip_nul_bytes(session, _ctx, _instances):
    from sqlalchemy import inspect as sa_inspect

    for obj in list(session.new) + list(session.dirty):
        for attr in sa_inspect(obj).mapper.column_attrs:
            value = getattr(obj, attr.key, None)
            if isinstance(value, (str, dict, list)):
                cleaned = strip_nul(value)
                if cleaned != value:
                    setattr(obj, attr.key, cleaned)


class EmbeddingType(TypeDecorator):
    """pgvector `vector(n)` on PostgreSQL, JSON list everywhere else (SQLite for local dev/tests)."""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(JSON())

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return [float(v) for v in value]


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    add_missing_columns()


def add_missing_columns() -> list[str]:
    """create_all makes new tables but never changes existing ones, so add any nullable column a model gained
    since the table was created (there are no migrations). Returns the "table.column" names it added."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or not col.nullable or col.primary_key:
                    continue
                kind = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {kind}'))
                for ix in table.indexes:
                    if [c.name for c in ix.columns] == [col.name]:
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS {ix.name} ON {table.name} ("{col.name}")'))
                added.append(f"{table.name}.{col.name}")
    return added


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
