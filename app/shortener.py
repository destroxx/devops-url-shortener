"""Core business logic: creating and resolving short links.

Kept separate from `main.py` on purpose. HTTP concerns (status codes, request
parsing) live in the route layer; this module knows nothing about HTTP and can
therefore be unit-tested without spinning up a web server.
"""

import logging
import secrets
import string

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ShortLink, _utcnow

logger = logging.getLogger(__name__)

# Base62: digits + lowercase + uppercase. No punctuation, so codes survive
# being pasted into chat apps, URLs, and shell commands without escaping.
ALPHABET = string.ascii_letters + string.digits

# Number of times we retry when a randomly generated code already exists.
MAX_GENERATION_ATTEMPTS = 5


def generate_short_code(length: int | None = None) -> str:
    """Return a cryptographically-random short code.

    We use `secrets`, NOT `random`.

    `random` is a Mersenne Twister: fast, statistically excellent, and fully
    PREDICTABLE. Given a few hundred outputs, an attacker can reconstruct its
    internal state and predict every future value. For a URL shortener that
    means enumerating everyone else's private links. `secrets` draws from the
    OS entropy pool and is designed to resist exactly that.

    Rule of thumb: if a value is a password, token, session id, or anything an
    attacker benefits from guessing, use `secrets`.
    """
    length = length or get_settings().short_code_length
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def create_short_link(db: Session, target_url: str) -> ShortLink:
    """Persist a new short link, retrying on the rare code collision.

    With a 7-character base62 code there are 62^7 ~= 3.5 trillion possibilities,
    so collisions are vanishingly rare — but "vanishingly rare" is not "never",
    and at scale rare events happen daily. We rely on the database's UNIQUE
    constraint as the source of truth rather than a check-then-insert, because
    two concurrent requests could both pass a check before either inserts.
    Letting the database arbitrate is the only race-free approach.
    """
    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        link = ShortLink(short_code=generate_short_code(), target_url=target_url)
        db.add(link)
        try:
            db.commit()
        except IntegrityError:
            # Someone else claimed this code between our generation and commit.
            db.rollback()
            logger.warning(
                "short_code collision on attempt %d/%d", attempt, MAX_GENERATION_ATTEMPTS
            )
            continue
        db.refresh(link)
        logger.info("created short_code=%s", link.short_code)
        return link

    # Exhausting all attempts means something is systemically wrong (for example
    # the code length is far too small for the amount of data stored). Fail loudly.
    raise RuntimeError(
        f"Could not generate a unique short code after {MAX_GENERATION_ATTEMPTS} attempts"
    )


def get_link_by_code(db: Session, short_code: str) -> ShortLink | None:
    """Look up a link by its code, or return None."""
    return db.query(ShortLink).filter(ShortLink.short_code == short_code).one_or_none()


def register_click(db: Session, link: ShortLink) -> None:
    """Record that a link was followed.

    Uses an atomic UPDATE ... SET click_count = click_count + 1 rather than
    reading the value into Python, adding one, and writing it back. The
    read-modify-write version silently loses counts under concurrency: two
    requests both read 5, both write 6, and one click disappears. Pushing the
    arithmetic into the database makes it atomic.
    """
    db.query(ShortLink).filter(ShortLink.id == link.id).update(
        {
            ShortLink.click_count: ShortLink.click_count + 1,
            ShortLink.last_accessed_at: _utcnow(),
        },
        synchronize_session=False,
    )
    db.commit()


def build_short_url(short_code: str) -> str:
    """Compose the full public URL for a code.

    The host comes from config, not from the incoming request's Host header.
    Trusting the Host header lets an attacker poison the URLs we hand out
    (a classic "host header injection"), so the value is pinned by env var.
    """
    return f"{get_settings().base_url.rstrip('/')}/{short_code}"
