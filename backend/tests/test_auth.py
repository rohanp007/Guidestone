"""Sign-in, sessions and ownership. The rule being tested: a signed-in customer can only ever see their own data."""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jwt
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, config
from app.main import app
from helpers import TEST_PASSWORD, email_of, session_for

BAD = {"detail": "Email or password is incorrect"}


@pytest.fixture(scope="module")
def anon():
    with TestClient(app) as c:
        yield c


def login(client, email, password=TEST_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# ------------------------------------------------------------------ login
def test_login_succeeds_and_sets_a_locked_down_cookie(anon):
    r = login(anon, "c002@example.com")
    assert r.status_code == 200 and r.json()["customer_id"] == "C002" and r.json()["first_name"]
    cookie = r.headers["set-cookie"]
    assert "gs_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Max-Age=28800" in cookie                                           # 8 hours
    assert TEST_PASSWORD not in r.text and "$argon2" not in r.text


def test_login_ignores_email_case_and_spaces(anon):
    assert login(anon, "  C002@Example.COM ").status_code == 200


def test_wrong_password_and_unknown_email_are_indistinguishable(anon):
    wrong = login(anon, "c002@example.com", "not the password")
    unknown = login(anon, "nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == BAD
    assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers


def test_cookie_is_secure_when_configured(anon, monkeypatch):
    base = config.auth_settings()
    monkeypatch.setattr(config, "auth_settings", lambda: replace(base, cookie_secure=True))
    assert "Secure" in login(anon, "c002@example.com").headers["set-cookie"]


def test_rate_limit_after_five_failures(anon):
    for _ in range(5):
        assert login(anon, "c002@example.com", "wrong").status_code == 401
    blocked = login(anon, "c002@example.com", "wrong")
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    assert login(anon, "c002@example.com").status_code == 429                  # even the right password, while blocked
    assert login(anon, "c067@example.com").status_code == 200                  # another email from the same address is fine


def test_rate_limit_also_caps_one_address_trying_many_emails(anon):
    for i in range(auth.MAX_FAILURES_PER_IP):
        assert login(anon, f"nobody{i}@example.com").status_code == 401
    assert login(anon, "c002@example.com").status_code == 429


def test_successful_logins_do_not_count_against_the_limit(anon):
    for _ in range(8):
        assert login(anon, "c002@example.com").status_code == 200


# ------------------------------------------------------------------ who is asking
PROTECTED_GET = ["/api/me", "/api/profiles", "/api/profiles/C002", "/api/profiles/C002/transactions", "/api/formulas", "/api/assumptions"]


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_no_cookie_is_401(anon, path):
    fresh = TestClient(app)
    assert fresh.get(path).status_code == 401


def test_no_cookie_cannot_run_a_formula():
    assert TestClient(app).post("/api/profiles/C002/formulas/paycheck_to_paycheck", json={}).status_code == 401


def test_health_and_login_are_the_only_open_routes():
    c = TestClient(app)
    assert c.get("/api/health").status_code == 200
    open_paths = {p for p, ops in app.openapi()["paths"].items() for m in ops if m == "get"}
    for p in open_paths - {"/api/health", "/api/auth/demo-personas"}:
        assert c.get(p.replace("{id}", "C002")).status_code in (401, 405), p


def test_forged_and_expired_and_unknown_tokens_are_401():
    secret = config.auth_settings().session_secret
    now = datetime.now(timezone.utc)
    forged = jwt.encode({"sub": "C002", "jti": "x", "exp": now + timedelta(hours=1)}, "some other secret that is long enough 1234", algorithm="HS256")
    auth._LIVE["expired-jti"] = ("C002", time.time() + 3600)
    expired = jwt.encode({"sub": "C002", "jti": "expired-jti", "exp": now - timedelta(seconds=5)}, secret, algorithm="HS256")
    never_issued = jwt.encode({"sub": "C002", "jti": "not-live", "exp": now + timedelta(hours=1)}, secret, algorithm="HS256")
    no_exp = jwt.encode({"sub": "C002", "jti": "expired-jti"}, secret, algorithm="HS256")
    none_alg = jwt.encode({"sub": "C002", "jti": "expired-jti", "exp": now + timedelta(hours=1)}, None, algorithm="none")
    for token in (forged, expired, never_issued, no_exp, none_alg, "garbage", ""):
        c = TestClient(app)
        c.cookies.set("gs_session", token)
        assert c.get("/api/me").status_code == 401, token[:20]


def test_me_returns_the_signed_in_customer_and_no_secrets():
    body = session_for("C002").get("/api/me").json()
    assert body["customer_id"] == "C002" and body["email"] == "c002@example.com" and body["first_name"] and body["last_name"]
    assert body["demo_mode"] is True and body["summary"]["net_worth"] and body["as_of_date"] == "2026-10-31"
    assert not any(k in body for k in ("password", "hash", "token"))


# ------------------------------------------------------------------ ownership
def test_c002_cannot_read_c067_anything():
    c = session_for("C002")
    assert c.get("/api/profiles/C002").status_code == 200
    assert c.get("/api/profiles/C067").status_code == 403
    assert c.get("/api/profiles/C067/transactions").status_code == 403
    assert c.get("/api/profiles/C067/transactions", params={"q": "a", "page_size": 5}).status_code == 403
    for f in c.get("/api/formulas").json():
        assert c.post(f"/api/profiles/C067/formulas/{f['name']}", json={}).status_code == 403, f["name"]
    assert c.get("/api/profiles/c067").status_code == 403                    # id case does not matter either
    assert c.get("/api/profiles/C067/").status_code in (307, 403, 404)


def test_forbidden_looks_the_same_for_real_and_made_up_customers():
    c = session_for("C002")
    real, fake = c.get("/api/profiles/C067"), c.get("/api/profiles/C999")
    assert real.status_code == fake.status_code == 403 and real.json() == fake.json()


def test_each_session_sees_only_its_own_data():
    a, b = session_for("C002"), session_for("C067")
    assert a.get("/api/profiles/C002/transactions").json()["items"][0]["account_id"].startswith("A-C002")
    assert b.get("/api/profiles/C067/transactions").json()["items"][0]["account_id"].startswith("A-C067")
    assert a.get("/api/me").json()["customer_id"] == "C002" and b.get("/api/me").json()["customer_id"] == "C067"


def test_formula_cache_is_not_shared_across_customers():
    a, b = session_for("C002"), session_for("C067")
    assert a.post("/api/profiles/C002/formulas/paycheck_to_paycheck", json={}).status_code == 200
    assert b.post("/api/profiles/C067/formulas/paycheck_to_paycheck", json={}).status_code == 200        # now cached for each
    assert a.post("/api/profiles/C067/formulas/paycheck_to_paycheck", json={}).status_code == 403


# ------------------------------------------------------------------ logout
def test_logout_ends_the_session_even_for_a_copied_cookie():
    c = session_for("C002")
    token = c.cookies.get("gs_session")
    assert c.get("/api/me").status_code == 200
    r = c.post("/api/auth/logout")
    assert r.status_code == 204 and "gs_session=" in r.headers["set-cookie"] and ("Max-Age=0" in r.headers["set-cookie"] or "expires" in r.headers["set-cookie"].lower())
    assert c.get("/api/me").status_code == 401
    replay = TestClient(app)
    replay.cookies.set("gs_session", token)                                  # someone who kept a copy of the cookie
    assert replay.get("/api/me").status_code == 401


def test_logout_without_a_session_is_harmless():
    assert TestClient(app).post("/api/auth/logout").status_code == 204


# ------------------------------------------------------------------ demo mode
def test_demo_personas_and_customer_list_exist_only_in_demo_mode(anon, monkeypatch):
    c = session_for("C002")
    personas = anon.get("/api/auth/demo-personas")
    assert personas.status_code == 200 and [p["customer_id"] for p in personas.json()] == config.FEATURED_PERSONAS
    assert all(set(p) == {"customer_id", "email", "first_name", "last_name"} for p in personas.json())    # no password, no hash
    assert c.get("/api/profiles").status_code == 200
    base = config.auth_settings()
    monkeypatch.setattr(config, "auth_settings", lambda: replace(base, demo_mode=False))
    assert anon.get("/api/auth/demo-personas").status_code == 404
    assert c.get("/api/profiles").status_code == 403
    assert c.get("/api/me").json()["demo_mode"] is False


# ------------------------------------------------------------------ configuration
def test_cors_allows_credentials_for_the_frontend_only(anon):
    ok = anon.options("/api/me", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000" and ok.headers["access-control-allow-credentials"] == "true"
    bad = anon.options("/api/me", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers      # no allowed origin: the browser blocks it


def test_every_customer_has_a_reserved_domain_login_and_a_name():
    users = pd.read_parquet(config.DATA_DIR / "users.parquet")
    assert len(users) == 100 and users["email"].is_unique
    assert users["email"].str.endswith("@example.com").all() and (users["email"] == users["customer_id"].str.lower() + "@example.com").all()
    assert (users["first_name"].str.len() > 0).all() and (users["last_name"].str.len() > 0).all()
    assert not users.duplicated(["first_name", "last_name"]).any()


def test_settings_refuse_weak_or_missing_secrets(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "short")
    config.auth_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="SESSION_SECRET"):
            config.auth_settings()
        monkeypatch.setenv("SESSION_SECRET", "x" * 40)
        monkeypatch.setenv("DEMO_PASSWORD_HASH", "plaintext-password")
        config.auth_settings.cache_clear()
        with pytest.raises(RuntimeError, match="argon2"):
            config.auth_settings()
    finally:
        monkeypatch.undo()
        config.auth_settings.cache_clear()
