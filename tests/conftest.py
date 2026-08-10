"""Shared pytest fixtures.

`conftest.py` is discovered automatically by pytest -- test files never import
it. Anything defined here is available to every test in this directory tree.

The central idea below is DEPENDENCY OVERRIDING. Our endpoints ask FastAPI for
a database session via `Depends(get_db)`. In tests we swap that dependency for
one backed by a throwaway in-memory database. The application code is not
modified or aware of this, which is exactly what makes it testable.
"""

import os

# These MUST be set before anything under `app` is imported, because
# `app.config` reads the environment at import time. Import order is load-
# bearing here; moving the app imports above this block would silently make the
# tests run against your real development database.
os.environ["ENVIRONMENT"] = "test"
os.environ["BASE_URL"] = "http://testserver"
os.environ["APP_VERSION"] = "test"
os.environ["DATABASE_URL"] = "sqlite://"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def db_engine():
    """A fresh, isolated, in-memory SQLite database for a single test.

    Two non-obvious details:

    1. `sqlite://` with no path means IN-MEMORY. Nothing touches disk, so the
       suite is fast and leaves no artefacts behind to pollute the next run.

    2. `StaticPool` is essential. By default SQLAlchemy's pool hands out a new
       connection per checkout -- and every new connection to `sqlite://` gets
       its OWN, EMPTY database. Your test would create a table on one
       connection and query a different, empty one, producing a baffling
       "no such table" error. StaticPool forces every checkout to reuse the
       single same connection, so there is exactly one in-memory database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    # Teardown: every test starts from a clean schema. Tests that depend on
    # leftovers from a previous test are the classic source of suites that
    # pass locally but fail in CI, where ordering differs.
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """A SQLAlchemy session bound to the throwaway test database."""
    factory = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_engine):
    """A TestClient whose `get_db` dependency points at the test database.

    TestClient drives the ASGI app IN-PROCESS. No socket is opened, no server
    process is started, nothing binds to a port. That is why these tests run in
    milliseconds and never fail because "port 8000 is already in use".
    """
    factory = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    # Always clean up. A leaked override would leak into unrelated tests and
    # cause failures that look impossible to explain.
    app.dependency_overrides.clear()


@pytest.fixture()
def sample_url() -> str:
    return "https://www.example.com/a/fairly/long/path?with=query&params=1"
