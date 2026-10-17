"""SQLAlchemy engine, session factory and table initialisation."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _enable_sqlite_foreign_keys(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_engine(url: str) -> Engine:
    """Create an engine. For a file-backed SQLite URL, the parent directory is created."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url)

    if parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


engine = create_db_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class SchemaMismatchError(RuntimeError):
    """The database was created by an older version of the models."""


def init_db(bind: Engine | None = None) -> None:
    """Create any missing tables, then check that existing tables match the models.

    Existing tables and rows are never altered. There are no migrations, so a database
    created by an older version of the models is refused with SchemaMismatchError at
    startup, instead of failing later on the first query.
    """
    from app.db import models  # noqa: F401  (registers models on Base.metadata)

    target = bind or engine
    Base.metadata.create_all(bind=target)
    _verify_schema(target)


def _verify_schema(target: Engine) -> None:
    inspector = inspect(target)
    problems = []
    for table in Base.metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        if missing := [column.name for column in table.columns if column.name not in existing]:
            problems.append(f"{table.name} lacks {', '.join(missing)}")
    if problems:
        raise SchemaMismatchError(
            f"The database {target.url.render_as_string(hide_password=True)} does not match this version of the "
            f"application ({'; '.join(problems)}). There are no migrations: back the file up if you need its data, "
            "delete it, then run `python -m app.db.seed`."
        )


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
