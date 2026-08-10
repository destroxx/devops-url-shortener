"""API schemas — the shape of data in flight.

Pydantic models that define the PUBLIC contract of the API. They do two jobs:

1. Validate and coerce incoming request bodies. Anything that fails validation
   is rejected with a 422 before a single line of our logic runs. This is a
   security boundary, not a convenience: it is how we avoid trusting input.

2. Filter outgoing responses to an explicit allowlist of fields. A database
   model gains columns over time; a response schema does not leak them.

FastAPI also reads these to auto-generate OpenAPI docs at /docs.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Schemes we are willing to redirect to. Allowing arbitrary schemes such as
# `javascript:` or `data:` would turn this service into an XSS delivery vector,
# and `file:` could probe the server's own filesystem. Allowlist, never blocklist.
ALLOWED_SCHEMES = {"http", "https"}


class ShortenRequest(BaseModel):
    """Body of POST /api/shorten."""

    url: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description="The absolute URL to shorten. Must be http or https.",
        examples=["https://www.example.com/some/very/long/path"],
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Reject anything that is not a plain http(s) absolute URL."""
        from urllib.parse import urlparse

        value = value.strip()
        parsed = urlparse(value)

        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            raise ValueError(
                f"URL scheme must be one of {sorted(ALLOWED_SCHEMES)}, got '{parsed.scheme}'"
            )
        if not parsed.netloc:
            raise ValueError("URL must include a host, e.g. https://example.com")
        return value


class ShortenResponse(BaseModel):
    """Body returned by POST /api/shorten."""

    short_code: str
    short_url: str
    target_url: str
    created_at: datetime

    # Lets FastAPI build this straight from a SQLAlchemy object by reading
    # attributes rather than dict keys.
    model_config = ConfigDict(from_attributes=True)


class StatsResponse(BaseModel):
    """Body returned by GET /api/stats/{short_code}."""

    short_code: str
    target_url: str
    click_count: int
    created_at: datetime
    last_accessed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """Body returned by the health and readiness probes.

    Deliberately tiny. Probes are hit every few seconds by Kubernetes forever;
    an expensive health check is a self-inflicted denial of service.
    """

    status: str
    version: str
    environment: str


class ErrorResponse(BaseModel):
    """Uniform error body, so clients can parse failures reliably."""

    detail: str
