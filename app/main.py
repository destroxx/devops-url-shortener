"""FastAPI application: routes, lifespan, observability, error handling.

This is the HTTP layer. It translates between the outside world and the pure
logic in `shortener.py`.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import shortener
from app.config import get_settings
from app.database import get_db, init_db
from app.schemas import (
    HealthResponse,
    ShortenRequest,
    ShortenResponse,
    StatsResponse,
)

settings = get_settings()

# --- Logging ------------------------------------------------------------------
# Log to STDOUT, never to a file. 12-Factor App, Factor XI: "Treat logs as event
# streams." A container should not manage log files, rotation, or disk space.
# It writes to stdout; the platform (Docker, Kubernetes, Fluent Bit) collects,
# ships, and retains. Writing to a file inside a container means the logs are
# destroyed the moment the pod is rescheduled.
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
)
logger = logging.getLogger("app")


# --- Prometheus metrics -------------------------------------------------------
# Prometheus works by SCRAPING: it periodically GETs /metrics and stores the
# numbers. The app does not push anywhere, which means it has no idea whether
# monitoring exists — a clean separation.
#
# The three metric types you will actually use:
#   Counter   - only ever goes up (total requests, total errors)
#   Gauge     - goes up and down (queue depth, memory in use)
#   Histogram - samples bucketed by value (latency), enabling percentiles
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests processed.",
    ["method", "endpoint", "status_code"],  # labels let you slice the data
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "endpoint"],
)
LINKS_CREATED = Counter("short_links_created_total", "Short links created.")
REDIRECTS_SERVED = Counter("redirects_served_total", "Redirects served.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup and shutdown hooks.

    Code before `yield` runs once at startup; code after runs at shutdown.
    Graceful shutdown matters in Kubernetes: when a pod is terminated it gets
    SIGTERM and a grace period to finish in-flight requests before SIGKILL.
    """
    logger.info(
        "starting %s version=%s environment=%s",
        settings.app_name,
        settings.app_version,
        settings.environment,
    )
    init_db()
    yield
    logger.info("shutting down %s", settings.app_name)


app = FastAPI(
    title="DevOps URL Shortener",
    description=(
        "A small URL shortener used to demonstrate a complete DevOps pipeline: "
        "containerisation, CI/CD, Kubernetes, IaC, and monitoring."
    ),
    version=settings.app_version,
    lifespan=lifespan,
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Time every request and record metrics.

    Middleware wraps every request, so instrumentation lives in exactly one
    place instead of being copy-pasted into each endpoint.
    """
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start

    # IMPORTANT: label by the ROUTE TEMPLATE ("/{short_code}"), never the raw
    # path ("/aB3xK9p"). Prometheus creates one time series per unique label
    # combination; using raw paths would create millions of series and take the
    # monitoring system down. This failure mode is called "cardinality explosion"
    # and it is the #1 way people break Prometheus.
    route = request.scope.get("route")
    endpoint = getattr(route, "path", "unmatched")

    REQUEST_COUNT.labels(request.method, endpoint, response.status_code).inc()
    REQUEST_LATENCY.labels(request.method, endpoint).observe(elapsed)

    # Expose timing to clients and log aggregators.
    response.headers["X-Process-Time-Ms"] = f"{elapsed * 1000:.2f}"
    return response


# --- Operational endpoints ----------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """LIVENESS probe. Answers only: "is this process alive and responsive?"

    It deliberately touches NO dependencies. If it checked the database, a
    transient DB outage would fail liveness on every pod at once and Kubernetes
    would restart the entire fleet, converting a brief blip into a real outage.
    """
    return HealthResponse(
        status="ok", version=settings.app_version, environment=settings.environment
    )


@app.get("/ready", response_model=HealthResponse, tags=["ops"])
def ready(db: Session = Depends(get_db)) -> HealthResponse:
    """READINESS probe. Answers: "can I serve real traffic right now?"

    This one DOES check the database, because an instance that cannot reach its
    database cannot serve requests. Failing readiness removes the pod from the
    load balancer without restarting it — exactly the right response to a
    dependency being temporarily unavailable.
    """
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - we genuinely want any failure here
        logger.error("readiness check failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    return HealthResponse(
        status="ready", version=settings.app_version, environment=settings.environment
    )


@app.get("/metrics", tags=["ops"], include_in_schema=False)
def metrics() -> Response:
    """Prometheus scrape endpoint, in Prometheus' text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Application endpoints ----------------------------------------------------


@app.post(
    "/api/shorten",
    response_model=ShortenResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["links"],
)
def shorten(payload: ShortenRequest, db: Session = Depends(get_db)) -> ShortenResponse:
    """Create a short link.

    Returns 201 Created, not 200 OK. Correct status codes are not pedantry:
    load balancers, caches, and client libraries all make retry and caching
    decisions based on them.
    """
    link = shortener.create_short_link(db, payload.url)
    LINKS_CREATED.inc()
    return ShortenResponse(
        short_code=link.short_code,
        short_url=shortener.build_short_url(link.short_code),
        target_url=link.target_url,
        created_at=link.created_at,
    )


@app.get("/api/stats/{short_code}", response_model=StatsResponse, tags=["links"])
def stats(short_code: str, db: Session = Depends(get_db)) -> StatsResponse:
    """Return click statistics for a short link."""
    link = shortener.get_link_by_code(db, short_code)
    if link is None:
        raise HTTPException(status_code=404, detail="short code not found")
    return StatsResponse.model_validate(link)


@app.get("/{short_code}", tags=["links"], include_in_schema=False)
def redirect(short_code: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Follow a short link.

    Uses 307 Temporary Redirect rather than 301 Permanent. A 301 is cached by
    browsers effectively forever, which would mean click counts stop updating
    and a mistyped target URL could never be corrected. 307 keeps control.
    """
    link = shortener.get_link_by_code(db, short_code)
    if link is None:
        raise HTTPException(status_code=404, detail="short code not found")

    shortener.register_click(db, link)
    REDIRECTS_SERVED.inc()
    return RedirectResponse(url=link.target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


# --- Error handling -----------------------------------------------------------


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all so unexpected errors never leak internals to the caller.

    A raw traceback in an HTTP response tells an attacker your file paths,
    library versions, and sometimes credentials. We log the full detail
    server-side and return a deliberately boring message to the client.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})
