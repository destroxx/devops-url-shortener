"""End-to-end API tests: a request goes in, a response comes out.

These exercise the real routing, validation, and database layers together.
They are the tests that give CI the confidence to deploy without a human.
"""

import pytest

# --- Operational endpoints ----------------------------------------------------


def test_health_returns_ok(client):
    """Liveness must be cheap and always succeed while the process is alive."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"


def test_ready_returns_ready_when_database_reachable(client):
    """Readiness verifies the database, so a healthy DB means a 200."""
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_metrics_endpoint_exposes_prometheus_format(client):
    """Prometheus needs its specific text exposition format, not JSON."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    # Our custom counters must be present in the output.
    assert "short_links_created_total" in response.text
    assert "http_requests_total" in response.text


def test_every_response_carries_timing_header(client):
    """The observability middleware must run for all requests."""
    response = client.get("/health")
    assert "x-process-time-ms" in response.headers
    assert float(response.headers["x-process-time-ms"]) >= 0


# --- Creating short links -----------------------------------------------------


def test_shorten_creates_link(client, sample_url):
    response = client.post("/api/shorten", json={"url": sample_url})

    # 201 Created, not 200. Status codes drive client and proxy behaviour.
    assert response.status_code == 201
    body = response.json()

    assert body["target_url"] == sample_url
    assert len(body["short_code"]) == 7
    assert body["short_url"] == f"http://testserver/{body['short_code']}"
    assert "created_at" in body


def test_shorten_produces_unique_codes(client, sample_url):
    """Two requests for the same URL still get distinct codes.

    This is a deliberate product decision worth understanding: we do NOT
    de-duplicate. Each caller gets their own code so their click statistics
    are independent of everyone else's.
    """
    codes = {
        client.post("/api/shorten", json={"url": sample_url}).json()["short_code"]
        for _ in range(10)
    }
    assert len(codes) == 10


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",  # XSS vector if we redirected to it
        "data:text/html,<script>alert(1)</script>",  # same
        "file:///etc/passwd",  # could probe the server's own filesystem
        "ftp://example.com/file",  # not a scheme browsers should follow here
        "not-a-url",  # no scheme at all
        "https://",  # scheme but no host
        "",  # empty
    ],
)
def test_shorten_rejects_dangerous_or_malformed_urls(client, bad_url):
    """Input validation is a security boundary, so it gets its own test matrix.

    Each of these would be a genuine vulnerability if accepted. Parametrising
    means one new line adds a new case -- cheap to extend when someone reports
    a new bypass.
    """
    response = client.post("/api/shorten", json={"url": bad_url})
    assert response.status_code == 422, f"{bad_url!r} should have been rejected"


def test_shorten_rejects_overlong_url(client):
    """Bound every input. Unbounded input is a denial-of-service vector."""
    response = client.post("/api/shorten", json={"url": "https://e.com/" + "a" * 3000})
    assert response.status_code == 422


def test_shorten_requires_url_field(client):
    response = client.post("/api/shorten", json={})
    assert response.status_code == 422


# --- Redirecting --------------------------------------------------------------


def test_redirect_sends_client_to_target(client, sample_url):
    code = client.post("/api/shorten", json={"url": sample_url}).json()["short_code"]

    # follow_redirects=False so we can inspect the redirect itself rather than
    # having httpx transparently follow it to a real external site.
    response = client.get(f"/{code}", follow_redirects=False)

    # 307, not 301: a permanent redirect would be cached by browsers forever,
    # freezing click counts and making a wrong target impossible to fix.
    assert response.status_code == 307
    assert response.headers["location"] == sample_url


def test_redirect_unknown_code_returns_404(client):
    response = client.get("/doesnotexist", follow_redirects=False)
    assert response.status_code == 404
    assert response.json()["detail"] == "short code not found"


# --- Statistics ---------------------------------------------------------------


def test_stats_starts_at_zero(client, sample_url):
    code = client.post("/api/shorten", json={"url": sample_url}).json()["short_code"]
    body = client.get(f"/api/stats/{code}").json()

    assert body["click_count"] == 0
    assert body["last_accessed_at"] is None
    assert body["target_url"] == sample_url


def test_stats_counts_every_click(client, sample_url):
    code = client.post("/api/shorten", json={"url": sample_url}).json()["short_code"]

    for _ in range(3):
        client.get(f"/{code}", follow_redirects=False)

    body = client.get(f"/api/stats/{code}").json()
    assert body["click_count"] == 3
    assert body["last_accessed_at"] is not None


def test_stats_unknown_code_returns_404(client):
    assert client.get("/api/stats/nope123").status_code == 404


def test_stats_never_leaks_internal_fields(client, sample_url):
    """The response schema is an allowlist; internal columns must not appear.

    This test is what stops a future developer's new `internal_notes` column
    from silently becoming public API.
    """
    code = client.post("/api/shorten", json={"url": sample_url}).json()["short_code"]
    body = client.get(f"/api/stats/{code}").json()

    assert set(body) == {
        "short_code",
        "target_url",
        "click_count",
        "created_at",
        "last_accessed_at",
    }
    assert "id" not in body
