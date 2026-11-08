"""/api/me/*: the signed-in customer's own data, with no customer id in the URL."""
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from app.main import app
from helpers import session_for

PATHS = ["/api/me/profile", "/api/me/transactions", "/api/me/forecast", "/api/me/status", "/api/me/recurring"]


def test_signed_out_is_401():
    c = TestClient(app)
    for p in PATHS:
        assert c.get(p).status_code == 401, p


def test_each_customer_gets_only_their_own_data():
    a, b = session_for("C002"), session_for("C067")
    assert a.get("/api/me/profile").json()["id"] == "C002" and b.get("/api/me/profile").json()["id"] == "C067"
    assert a.get("/api/me/transactions").json()["items"][0]["account_id"].startswith("A-C002")
    assert b.get("/api/me/forecast").json()["profile_id"] == "C067"
    assert a.get("/api/me/profile", params={"id": "C067"}).json()["id"] == "C002"     # a stray id is ignored, never honoured


def test_status_has_a_plain_label():
    s = session_for("C067").get("/api/me/status").json()
    assert s["status"] in ("on_track", "watch", "at_risk") and s["label"] and "_" not in s["label"]


def test_forecast_covers_180_days():
    f = session_for("C002").get("/api/me/forecast").json()
    assert f["horizon_months"] == 6 and len(f["daily"]["p50"]) == 91 and f["monthly"]


def test_recurring_excludes_transfers_and_dates_are_in_the_future():
    c = session_for("C067")
    items = c.get("/api/me/recurring").json()
    names = [i["name"] for i in items]
    assert "Transfer to Savings" not in names and any(i["group"] == "income" for i in items) and any(i["group"] == "bill" for i in items)
    assert all(date.fromisoformat(i["next_date"]) > date(2026, 10, 31) and i["amount"] > 0 for i in items)
