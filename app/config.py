"""Application configuration.

12-Factor App, Factor III: "Store config in the environment."

Nothing in this file contains a real secret or an environment-specific value.
Every setting is read from an environment variable, with a safe default that
works for local development only. Production supplies real values via:
  - docker-compose  -> `environment:` block
  - Kubernetes      -> ConfigMap (non-secret) and Secret (passwords)

This is why ONE container image can run in dev, staging, and prod unchanged.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings are populated from environment variables automatically.

    pydantic-settings matches each field to an env var of the same name,
    case-insensitively. `database_url` <- DATABASE_URL. It also *validates*
    and *type-casts*: if someone sets PORT=banana, the app refuses to start
    with a clear error instead of failing mysteriously at runtime.

    Failing fast at startup is a DevOps virtue: a container that crashes
    immediately is far easier to diagnose than one that misbehaves at 3am.
    """

    model_config = SettingsConfigDict(
        env_file=".env",  # convenient for local dev; never committed to git
        env_file_encoding="utf-8",
        extra="ignore",  # tolerate unrelated env vars (k8s injects many)
    )

    # --- Application identity -------------------------------------------------
    app_name: str = "url-shortener"
    # Set from the git SHA at build time so a running container can tell you
    # exactly which commit it was built from. Invaluable during an incident.
    app_version: str = "dev"
    environment: str = "local"  # local | ci | staging | production

    # --- Database -------------------------------------------------------------
    # Default is SQLite so the app runs with zero setup. Docker Compose and
    # Kubernetes both override this with a real PostgreSQL URL.
    database_url: str = "sqlite:///./shortener.db"

    # --- HTTP -----------------------------------------------------------------
    # The public base used to build short URLs we hand back to callers.
    base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # --- Behaviour ------------------------------------------------------------
    short_code_length: int = 7


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Reading and validating env vars is cheap but not free, and we want one
    consistent view of config for the process lifetime. `lru_cache` makes this
    a singleton. It also gives us a clean seam for tests, which can call
    `get_settings.cache_clear()` to pick up patched environment variables.
    """
    return Settings()
