"""Database models — the shape of data at rest.

These classes map 1:1 to database tables. They are INTERNAL. They are never
returned directly from an endpoint; `schemas.py` defines the public shape.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp.

    ALWAYS store UTC, never local time. Your laptop is in IST, the CI runner is
    in UTC, and the production node might be in us-east-1. Storing local time
    makes "which event happened first?" unanswerable, and daylight-saving
    transitions create duplicate or missing hours once a year.
    """
    return datetime.now(UTC)


class ShortLink(Base):
    """A single shortened URL and its click statistics."""

    __tablename__ = "short_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # The generated slug, e.g. "aB3xK9p". Indexed and unique because every
    # redirect is a lookup on this column — without an index, each redirect
    # would scan the whole table, and latency would grow linearly with data.
    short_code: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, nullable=False
    )

    # 2048 chars is the pragmatic maximum URL length supported across browsers.
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<ShortLink {self.short_code} -> {self.target_url[:40]}>"
