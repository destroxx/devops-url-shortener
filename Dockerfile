# syntax=docker/dockerfile:1.7
# ==============================================================================
# MULTI-STAGE BUILD
#
# Two stages. The "builder" stage installs compilers and build tooling to
# produce a virtual environment. The "runtime" stage starts from a clean base
# and copies ONLY that finished virtualenv across.
#
# Everything in the builder -- gcc, header files, pip's cache, the build deps --
# is discarded. Two wins:
#   * SIZE: the final image is a fraction of a single-stage build.
#   * SECURITY: a compiler inside a production container is a gift to anyone who
#     manages to get a shell in it. If it isn't there, it can't be used.
# ==============================================================================


# ------------------------------------------------------------------------------
# STAGE 1: builder
# ------------------------------------------------------------------------------
# Base image choice, briefly:
#   python:3.12          ~1GB   full Debian. Convenient, wastefully large.
#   python:3.12-slim     ~150MB Debian minus docs/locales/extras.  <-- we use this
#   python:3.12-alpine   ~50MB  musl libc instead of glibc. Tempting, but many
#                               Python wheels are built for glibc only, so pip
#                               falls back to COMPILING FROM SOURCE -- slower
#                               builds, and subtle runtime differences.
#   gcr.io/distroless    ~50MB  no shell, no package manager. Most secure, and
#                               the hardest to debug. A good next step later.
#
# The tag is pinned to an exact patch (3.12.8), never `python:3.12` or `latest`.
# A floating tag means today's build and next month's build produce different
# images from the same commit -- the reproducibility problem again, one level up
# from pinned pip requirements.
FROM python:3.14.0-slim-bookworm AS builder

# PYTHONDONTWRITEBYTECODE: skip .pyc files. They add image weight and are
#   useless in a container that is rebuilt rather than re-run from cache.
# PYTHONUNBUFFERED: THIS ONE IS CRITICAL IN CONTAINERS. Python buffers stdout
#   when it is not a TTY -- which is exactly the case under Docker/Kubernetes.
#   Without it, your logs sit in a 4KB buffer and appear minutes late, or vanish
#   entirely if the process crashes. You will debug a "silent" container for an
#   hour before discovering the logs were simply never flushed.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-time system packages.
#   build-essential : gcc and friends, needed if any dependency lacks a wheel
#   libpq-dev       : PostgreSQL client headers for psycopg2
#
# Note the single RUN with `&&`. Each RUN creates a layer, so splitting these
# would create three layers and -- worse -- `rm -rf /var/lib/apt/lists/*` in a
# LATER layer would not shrink the image at all. Deleting a file in a subsequent
# layer only masks it; the bytes remain in the layer beneath. Clean up in the
# SAME layer that created the mess.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Build into a virtualenv so the runtime stage can copy one self-contained
# directory instead of hunting through system site-packages.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# ------------------------------------------------------------------------------
# THE LAYER-CACHING RULE, APPLIED
#
# Copy ONLY requirements.txt, then install. Because this layer's inputs are just
# that one file, editing application code does NOT invalidate it -- Docker
# reuses the cached dependency install.
#
# If we instead wrote `COPY . .` before `pip install`, every one-character edit
# to main.py would re-download and re-install every dependency. That is the
# difference between a 2-second rebuild and a 90-second one, on every save.
# ------------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ------------------------------------------------------------------------------
# STAGE 2: runtime
# ------------------------------------------------------------------------------
FROM python:3.14.0-slim-bookworm AS runtime

# OCI standard labels. Registries, security scanners, and `docker inspect` read
# these. `revision` in particular lets you map a running container back to the
# exact commit that produced it -- the first question asked in any incident.
ARG APP_VERSION=dev
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="devops-url-shortener" \
      org.opencontainers.image.description="URL shortener demonstrating a full DevOps pipeline" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_VERSION="${APP_VERSION}"

# RUNTIME system packages only.
# libpq5 is the PostgreSQL client LIBRARY; libpq-dev (the headers, plus gcc)
# stays behind in the builder stage. Headers are needed to compile, not to run.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------------------
# THE MOST IMPORTANT SECURITY LINE IN THIS FILE
#
# Containers run as root (UID 0) unless told otherwise, and that is the HOST's
# root, mapped through the namespace. Because containers share the host kernel,
# a container escape from a root process is a full host compromise.
#
# `--system` creates an account with no password, no home directory, and no
# login shell -- nothing to log into.
# ------------------------------------------------------------------------------
RUN groupadd --system --gid 1001 appgroup \
    && useradd --system --uid 1001 --gid appgroup --no-create-home appuser

WORKDIR /app

# Bring across the finished virtualenv. gcc, headers, and apt caches stay behind.
COPY --from=builder --chown=appuser:appgroup /opt/venv /opt/venv

# Application code last: it changes most often, so it is the cheapest layer to
# rebuild. --chown avoids a second layer just to fix file ownership.
COPY --chown=appuser:appgroup app/ ./app/

# Drop privileges. Every instruction after this, and the container's main
# process, runs as an unprivileged user.
USER appuser

# EXPOSE is DOCUMENTATION ONLY -- it publishes nothing and opens no port. It
# records intent for humans and tooling. Actual publishing is `-p 8000:8000`
# at run time, or a Service in Kubernetes.
EXPOSE 8000

# Container-level health check. Docker marks the container healthy/unhealthy,
# and `docker compose` can wait on it before starting dependent services.
#
# We use Python's urllib rather than curl because slim images have no curl, and
# installing one purely for health checks adds weight and attack surface.
#
# In Kubernetes this is IGNORED -- the kubelet runs its own liveness and
# readiness probes instead (see k8s/deployment.yaml). It remains useful for
# local Compose runs.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status==200 else sys.exit(1)"

# ------------------------------------------------------------------------------
# CMD in EXEC form (a JSON array), never shell form.
#
#   Shell form  CMD uvicorn app.main:app
#     -> runs `/bin/sh -c "uvicorn ..."`. The SHELL becomes PID 1 and does not
#        forward signals. `docker stop` sends SIGTERM, the shell ignores it,
#        nothing reaches uvicorn, and 10 seconds later Docker sends SIGKILL.
#        Every in-flight request is severed. In Kubernetes this means every
#        rolling deploy drops connections.
#
#   Exec form   CMD ["uvicorn", ...]
#     -> uvicorn IS PID 1 and receives SIGTERM directly, so it can stop
#        accepting new connections, finish in-flight ones, and exit cleanly.
#
# --workers 1 is deliberate: in Kubernetes you scale by adding PODS, not
# in-process workers. One process per container keeps metrics, logs, memory
# limits, and crash semantics simple -- and lets the scheduler do its job.
# ------------------------------------------------------------------------------
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
