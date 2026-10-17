import os
import tempfile

import pytest

# Point the app at a throwaway SQLite database before any app module is imported.
_tmpdir = tempfile.mkdtemp(prefix="invoice-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["UPLOAD_DIR"] = f"{_tmpdir}/uploads"


@pytest.fixture
def client(db_engine):
    """API client whose requests use the test's own database."""
    from tests.helpers import api_client

    with api_client(db_engine) as c:
        yield c


@pytest.fixture
def db_url(tmp_path):
    """URL of a fresh, file-backed SQLite database unique to the test."""
    return f"sqlite:///{tmp_path / 'app.db'}"


@pytest.fixture
def db_engine(db_url):
    from app.db.database import create_db_engine, init_db

    engine = create_db_engine(db_url)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    from sqlalchemy.orm import Session

    with Session(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def seeded_db(db_session):
    """A session on a fresh database holding the seed purchase orders."""
    from app.db.seed import seed_purchase_orders

    seed_purchase_orders(db_session)
    return db_session
