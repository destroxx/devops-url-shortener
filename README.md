# DevOps URL Shortener

A small URL-shortening service used as a vehicle for a **complete, production-shaped
DevOps pipeline**: containerisation, CI/CD, Kubernetes, infrastructure as code, and
monitoring.

The application is deliberately simple. The interesting part is everything wrapped
around it.

---

## What it does

| Method | Path                     | Purpose                                  |
| ------ | ------------------------ | ---------------------------------------- |
| `POST` | `/api/shorten`           | Create a short link                      |
| `GET`  | `/{short_code}`          | Redirect to the target URL (307)         |
| `GET`  | `/api/stats/{short_code}`| Click statistics for a link              |
| `GET`  | `/health`                | Liveness probe — is the process alive?   |
| `GET`  | `/ready`                 | Readiness probe — can it serve traffic?  |
| `GET`  | `/metrics`               | Prometheus metrics                       |
| `GET`  | `/docs`                  | Auto-generated interactive API docs      |

---

## Quick start

### Option 1 — Docker Compose (recommended)

Brings up the app plus PostgreSQL, networked and configured:

```bash
docker compose up --build
```

Then open <http://localhost:8000/docs>.

```bash
docker compose down       # stop, keep the database volume
docker compose down -v    # stop and DELETE the database volume
```

### Option 2 — Locally with a virtualenv

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env      # optional; defaults work out of the box
uvicorn app.main:app --reload
```

Defaults to SQLite, so no database server is required.

### Try it

```bash
curl -X POST http://localhost:8000/api/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://github.com"}'
```

---

## Development

```bash
pytest                              # tests + coverage (fails under 80%)
ruff check .                        # lint
ruff check --fix .                  # lint and auto-fix
bandit -c pyproject.toml -r app     # security scan of our code
pip-audit -r requirements.txt       # CVE scan of our dependencies
```

---

## Project layout

```
.
├── app/                    Application source
│   ├── config.py           Environment-driven settings (12-factor)
│   ├── database.py         Engine, connection pooling, session-per-request
│   ├── models.py           SQLAlchemy models — data at rest
│   ├── schemas.py          Pydantic schemas — data in flight (API contract)
│   ├── shortener.py        Business logic, no HTTP knowledge
│   └── main.py             Routes, probes, metrics, error handling
├── tests/                  Unit and integration tests
├── .github/workflows/      CI/CD pipeline
├── k8s/                    Kubernetes manifests
├── terraform/              Infrastructure as code
├── monitoring/             Prometheus and Grafana configuration
├── docs/                   Written walkthrough of each phase
├── Dockerfile              Multi-stage, non-root production image
└── docker-compose.yml      Local development stack
```

---

## Configuration

Every setting is read from an environment variable. Nothing environment-specific
is baked into the code or the image, which is what allows one image to run
unchanged in local, staging, and production.

| Variable            | Default                      | Purpose                          |
| ------------------- | ---------------------------- | -------------------------------- |
| `DATABASE_URL`      | `sqlite:///./shortener.db`   | Database connection string       |
| `BASE_URL`          | `http://localhost:8000`      | Public base for generated links  |
| `ENVIRONMENT`       | `local`                      | `local`/`ci`/`staging`/`production` |
| `APP_VERSION`       | `dev`                        | Set to the git SHA at build time |
| `LOG_LEVEL`         | `INFO`                       | Python log level                 |
| `SHORT_CODE_LENGTH` | `7`                          | Generated code length            |

See `.env.example`. Copy it to `.env` for local use — `.env` is gitignored and
must never be committed.

---

## Design decisions worth knowing

**Liveness and readiness are separate, and liveness never touches the database.**
If liveness checked the database, one brief database blip would fail the probe on
every pod simultaneously and Kubernetes would restart the entire fleet — turning a
short dependency hiccup into a full outage. Readiness handles that case correctly
by removing pods from the load balancer without restarting them.

**Short codes come from `secrets`, not `random`.** `random` is a Mersenne Twister:
fast, and fully predictable from a few hundred observed outputs. For a URL
shortener that would mean enumerating other people's links.

**Redirects are 307, not 301.** A permanent redirect is cached by browsers
effectively forever, which freezes click counts and makes a mistyped target
impossible to correct.

**Click counts are incremented in SQL, not in Python.** A read-modify-write loses
counts under concurrency; `SET click_count = click_count + 1` is atomic.

**Models and schemas are separate types.** The response schema is an explicit
allowlist, so a column added to the database later cannot silently become public
API.

**Metrics are labelled by route template, never raw path.** Labelling by
`/{short_code}` keeps the number of time series bounded. Using the raw path would
create one series per short link — a cardinality explosion that takes Prometheus
down.

---

## Learning path

`docs/` contains a written walkthrough of each phase, in order:

1. The application — 12-factor config, testing, linting
2. Containerisation — layers, caching, multi-stage builds, non-root
3. CI/CD — automated testing, scanning, and publishing
4. Kubernetes — deployments, services, probes, scaling
5. Infrastructure as code — Terraform
6. Monitoring — Prometheus and Grafana

---

## Licence

MIT
