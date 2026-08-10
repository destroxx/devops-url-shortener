"""Database engine, session management, and the FastAPI dependency.

Key DevOps concepts demonstrated here:

1. The database is chosen entirely by the DATABASE_URL env var. SQLite locally
   and in CI; PostgreSQL in Docker Compose and Kubernetes. Same code.

2. Connection pooling. Establishing a TCP connection + TLS + auth to Postgres
   costs tens of milliseconds. A pool keeps connections open and hands them
   out, which is the difference between 50 req/s and 5000 req/s.

3. `pool_pre_ping`. In Kubernetes, pods are restarted and network paths are
   re-routed constantly. A pooled connection can go stale without the client
   noticing, producing confusing "server closed the connection" errors.
   pre_ping issues a cheap SELECT 1 before handing out a connection, and
   transparently replaces it if dead. This single flag prevents a whole class
   of flaky production incidents.
"""

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class Base(DeclarativeBase):
    """Base class every ORM model inherits from.

    SQLAlchemy collects table definitions on `Base.metadata`, which is what
    lets us create every table with a single call.
    """


def _build_engine():
    """Create the SQLAlchemy engine, tuned for the target database."""
    url = settings.database_url
    connect_args: dict = {}
    kwargs: dict = {
        # Verify a pooled connection is alive before using it. Cheap insurance.
        "pool_pre_ping": True,
        # Log every SQL statement when running at DEBUG. Extremely useful when
        # diagnosing a slow endpoint, and noisy enough that it stays off by default.
        "echo": settings.log_level.upper() == "DEBUG",
    }

    if url.startswith("sqlite"):
        # SQLite refuses to use a connection across threads by default, but
        # FastAPI serves requests from a thread pool. This flag relaxes that;
        # it is safe because we use one short-lived session per request.
        connect_args["check_same_thread"] = False
    else:
        # Pool sizing only makes sense for a real client/server database.
        # pool_size      = connections kept open per process
        # max_overflow   = extra connections allowed during a burst
        # pool_recycle   = drop connections older than this many seconds, so we
        #                  never hit Postgres' or a load balancer's idle timeout
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)

    return create_engine(url, connect_args=connect_args, **kwargs)


engine = _build_engine()

# A session factory. Each HTTP request gets its own Session — a unit of work
# that is opened, used, and closed. Sharing one session across requests is a
# classic source of data corruption under concurrency.
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        """SQLite ignores FOREIGN KEY constraints unless explicitly enabled.

        Without this, tests would happily accept data that PostgreSQL rejects
        in production — the worst kind of environment drift, because CI stays
        green while prod breaks.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session per request.

    FastAPI calls this for any endpoint declaring `db: Session = Depends(get_db)`.
    The `try/finally` guarantees the connection returns to the pool even if the
    endpoint raises. Leaking connections exhausts the pool and hangs the service
    — a genuinely common production outage.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables that do not yet exist.

    NOTE: this is deliberately simple, and appropriate for a learning project.
    Real production systems use a migration tool (Alembic) instead, because
    `create_all` can only ADD tables — it can never alter or drop a column, so
    it cannot evolve a schema that already holds data.
    """
    from app import models  # noqa: F401  -- import registers models on Base.metadata

    Base.metadata.create_all(bind=engine)
    logger.info("Database schema ensured at %s", _sanitize(settings.database_url))


def _sanitize(url: str) -> str:
    """Strip credentials out of a database URL before logging it.

    Logs get shipped to aggregators, screenshotted, and pasted into tickets.
    A password in a log line is a security incident. Never log a raw DSN.
    """
    if "@" in url and "//" in url:
        scheme, _, rest = url.partition("//")
        _, _, host = rest.rpartition("@")
        return f"{scheme}//***:***@{host}"
    return url
