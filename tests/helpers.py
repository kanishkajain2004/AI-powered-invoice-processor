"""Shared test helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker


@contextmanager
def api_client(engine: Engine, **kwargs) -> Iterator[TestClient]:
    """A TestClient whose requests use `engine` instead of the configured database."""
    from app.db import get_db
    from app.main import app

    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        with make_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app, **kwargs) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
