"""Unit tests for the business logic in `app/shortener.py`.

These bypass HTTP entirely. The distinction matters:

  UNIT tests (this file)  - one function, no web layer. Fast, pinpoint failures.
  INTEGRATION tests (test_api.py) - the whole stack together. Slower, but they
                            catch wiring mistakes a unit test never could.

You want a lot of the first and enough of the second. That shape is usually
drawn as the "testing pyramid".
"""

import string

import pytest

from app import shortener
from app.models import ShortLink


def test_generated_code_has_configured_length():
    assert len(shortener.generate_short_code()) == 7


def test_generated_code_length_is_overridable():
    assert len(shortener.generate_short_code(length=12)) == 12


def test_generated_code_uses_only_url_safe_characters():
    """No punctuation, so codes survive being pasted anywhere without escaping."""
    allowed = set(string.ascii_letters + string.digits)
    for _ in range(200):
        assert set(shortener.generate_short_code()) <= allowed


def test_generated_codes_are_effectively_unique():
    """A weak generator would show collisions almost immediately at this volume.

    This is a probabilistic smoke test, not a proof. It would reliably catch
    someone swapping `secrets.choice` for something badly broken, such as a
    generator seeded identically on every call.
    """
    codes = {shortener.generate_short_code() for _ in range(5000)}
    assert len(codes) == 5000


def test_create_short_link_persists_row(db_session):
    link = shortener.create_short_link(db_session, "https://example.com")

    assert link.id is not None
    assert link.click_count == 0
    assert link.created_at is not None

    stored = db_session.query(ShortLink).one()
    assert stored.short_code == link.short_code


def test_get_link_by_code_roundtrip(db_session):
    created = shortener.create_short_link(db_session, "https://example.com")
    found = shortener.get_link_by_code(db_session, created.short_code)
    assert found is not None
    assert found.id == created.id


def test_get_link_by_code_returns_none_when_missing(db_session):
    assert shortener.get_link_by_code(db_session, "missing") is None


def test_register_click_increments_atomically(db_session):
    link = shortener.create_short_link(db_session, "https://example.com")

    for _ in range(5):
        shortener.register_click(db_session, link)

    refreshed = shortener.get_link_by_code(db_session, link.short_code)
    assert refreshed.click_count == 5
    assert refreshed.last_accessed_at is not None


def test_create_short_link_retries_past_a_collision(db_session, monkeypatch):
    """Force a collision to prove the retry loop actually works.

    Collisions are astronomically rare in practice (62^7 possibilities), so the
    only way to test this path is to make the generator misbehave on purpose.
    Never leave error-handling code untested just because it is hard to trigger
    -- untested error paths are exactly the ones that fail during an incident.
    """
    calls: list[str] = []
    real_generate = shortener.generate_short_code

    def fake_generate(*args, **kwargs):
        # Return the same code twice to force one IntegrityError, then behave.
        if len(calls) < 2:
            calls.append("dup")
            return "AAAAAAA"
        return real_generate(*args, **kwargs)

    monkeypatch.setattr(shortener, "generate_short_code", fake_generate)

    first = shortener.create_short_link(db_session, "https://one.example.com")
    second = shortener.create_short_link(db_session, "https://two.example.com")

    assert first.short_code == "AAAAAAA"
    assert second.short_code != "AAAAAAA"  # collided, retried, succeeded


def test_create_short_link_gives_up_after_max_attempts(db_session, monkeypatch):
    """When every attempt collides we must fail loudly, not loop forever."""
    monkeypatch.setattr(shortener, "generate_short_code", lambda *a, **k: "SAMECODE")

    shortener.create_short_link(db_session, "https://first.example.com")

    with pytest.raises(RuntimeError, match="unique short code"):
        shortener.create_short_link(db_session, "https://second.example.com")


def test_build_short_url_uses_configured_base(monkeypatch):
    """The public base URL comes from config, never from the request's Host
    header -- trusting that header enables host-header injection."""
    assert shortener.build_short_url("abc1234") == "http://testserver/abc1234"
