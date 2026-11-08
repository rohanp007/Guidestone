"""Shared test helpers. Imports the app lazily: conftest sets the auth environment first."""
from __future__ import annotations

TEST_PASSWORD = "test-password-for-ci"


def email_of(customer_id: str) -> str:
    return f"{customer_id.lower()}@example.com"


def session_for(customer_id: str):
    """A client with its own cookie jar, signed in as that customer."""
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email_of(customer_id), "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return c
