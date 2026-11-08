"""API tests, through FastAPI's TestClient (the app's lifespan runs, so the data really loads)."""
from __future__ import annotations

import json
import math

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config
from app.data.loader import load_profiles, transactions_for
from app.formulas.forecast import forecast_liquid_summary
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def strict(resp):
    """Parse like a browser would: NaN / Infinity are not JSON, so they fail the test."""
    def reject(constant):
        raise AssertionError(f"response contains the non-JSON constant {constant}")
    return json.loads(resp.text, parse_constant=reject)


# ------------------------------------------------------------------ meta
def test_health_reports_100_profiles(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["profiles_loaded"] == 100
    assert body["as_of"] == "2026-10-31" and len(body["data_hash"]) == 12


def test_assumptions_have_sources_and_calibration(client):
    body = client.get("/api/assumptions").json()
    assert {"inflation_annual", "raise_annual", "ss_cola_annual", "rent_renewal_bump", "forecast_paths"} <= set(body["assumptions"])
    assert all({"value", "source", "as_of"} <= set(a) for a in body["assumptions"].values())
    assert "held-out" in body["calibration"] or "held out" in body["calibration"]
    assert body["calibration_details"]["nominal"] == 0.8


def test_openapi_lists_every_endpoint_and_docs_render(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ["/api/health", "/api/assumptions", "/api/profiles", "/api/profiles/{id}", "/api/profiles/{id}/transactions",
              "/api/formulas", "/api/profiles/{id}/formulas/{name}"]:
        assert p in paths, p
    assert client.get("/docs").status_code == 200
    schemas = client.get("/openapi.json").json()["components"]["schemas"]       # formula args and results are published too
    assert {"LiquidForecast", "ForecastArgs", "PaycheckToPaycheck", "Profile", "TransactionPage", "Shortfall", "Backtest"} <= set(schemas)


def test_cors_allows_only_the_local_frontend(client):
    ask = lambda origin: client.options("/api/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    assert ask("http://localhost:3000").headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "access-control-allow-origin" not in ask("http://evil.example").headers


# ------------------------------------------------------------------ profiles
def test_profile_list_puts_featured_personas_first(client, monkeypatch):
    monkeypatch.setattr(config, "FEATURED_PERSONAS", ["C050", "C003"])
    items = client.get("/api/profiles").json()
    assert len(items) == 100
    assert [i["id"] for i in items[:2]] == ["C050", "C003"]
    assert all("featured" in i["flags"] for i in items[:2]) and not any("featured" in i["flags"] for i in items[2:])
    rest = [i["id"] for i in items[2:]]
    assert rest == sorted(rest)
    assert {"id", "age", "employment", "housing", "credit_tier", "net_worth", "monthly_surplus", "flags"} == set(items[0])
    assert any("paycheck_to_paycheck" in i["flags"] for i in items) and any("revolver" in i["flags"] for i in items)


def test_profile_detail_matches_the_loader(client):
    body = strict(client.get("/api/profiles/C001"))
    s = load_profiles()["C001"].summary
    for field in ("liquid_cash", "total_assets", "total_debt", "net_worth", "monthly_surplus", "credit_utilization"):
        assert body["summary"][field] == pytest.approx(getattr(s, field))
    assert len(body["history"]) == 24 and body["spending"] is not None and body["recurring"]
    assert {a["type"] for a in body["accounts"]} >= {"checking", "credit_card", "mortgage", "home"}     # estimated assets included


def test_unknown_profile_is_404_everywhere(client):
    r = client.get("/api/profiles/C999")
    assert r.status_code == 404 and r.json() == {"detail": "profile C999 not found"}
    assert client.get("/api/profiles/C999/transactions").status_code == 404
    assert client.post("/api/profiles/C999/formulas/forecast_liquid", json={}).status_code == 404


# ------------------------------------------------------------------ transactions
def page(client, **params):
    r = client.get("/api/profiles/C001/transactions", params=params)
    assert r.status_code == 200, r.text
    return strict(r)


def test_transactions_paginate_and_totals_add_up(client):
    df = transactions_for("C001")
    first = page(client, page_size=200)
    assert first["total"] == len(df) and first["page_size"] == 200
    seen, p = [], 1
    while True:
        body = page(client, page=p, page_size=200)
        seen += body["items"]
        if p * 200 >= body["total"]: break
        p += 1
    assert len(seen) == len(df) and len({t["transaction_id"] for t in seen}) == len(df)    # every row once, none repeated
    assert page(client, page=10_000)["items"] == []


def test_transactions_are_newest_first_and_plain_json(client):
    items = page(client, page_size=200)["items"]
    dates = [t["posted_date"] for t in items]
    assert dates == sorted(dates, reverse=True) and len(dates[0]) == 10 and dates[0][4] == "-"      # ISO strings
    assert all(isinstance(t["amount"], float) and isinstance(t["balance_after"], float) for t in items)
    assert any(t["transfer_id"] is None for t in items) and any(t["transfer_id"] for t in items)


def test_page_size_is_capped(client):
    body = page(client, page_size=1000)
    assert body["page_size"] == 200 and len(body["items"]) == 200
    assert page(client)["page_size"] == 50 and len(page(client)["items"]) == 50


def test_transaction_filters(client):
    df = transactions_for("C001")
    body = page(client, account_id="A-C001-CHECKING", page_size=200)
    assert body["total"] == (df["account_id"] == "A-C001-CHECKING").sum() and {t["account_id"] for t in body["items"]} == {"A-C001-CHECKING"}
    body = page(client, category="housing", flow_class="expense", page_size=200)
    assert body["total"] == ((df["category"] == "housing") & (df["flow_class"] == "expense")).sum()
    assert {t["category"] for t in body["items"]} == {"housing"}
    body = page(client, start="2026-09-01", end="2026-09-30", page_size=200)
    in_range = df[(df["posted_date"] >= "2026-09-01") & (df["posted_date"] <= "2026-09-30")]
    assert body["total"] == len(in_range) and all("2026-09-01" <= t["posted_date"] <= "2026-09-30" for t in body["items"])
    one_day = page(client, start="2026-09-21", end="2026-09-21", page_size=200)
    assert {t["posted_date"] for t in one_day["items"]} <= {"2026-09-21"}                 # `end` is inclusive
    body = page(client, q="MORTGAGE", page_size=200)                                      # case-insensitive
    assert body["total"] == df["description"].str.contains("mortgage", case=False).sum() > 0
    assert all("mortgage" in t["description"].lower() for t in body["items"])
    assert page(client, q="no such merchant")["total"] == 0


def test_transaction_query_validation(client):
    for params in ({"page": 0}, {"page_size": 0}, {"start": "not-a-date"}):
        assert client.get("/api/profiles/C001/transactions", params=params).status_code == 422


def test_transactions_do_not_mutate_the_cached_frame(client):
    before = transactions_for("C001").copy()
    page(client, q="a", page_size=200)
    pd.testing.assert_frame_equal(transactions_for("C001"), before)


# ------------------------------------------------------------------ formulas
def test_formulas_are_listed_with_schemas(client):
    formulas = {f["name"]: f for f in client.get("/api/formulas").json()}
    assert {"forecast_liquid", "forecast_liquid_summary", "paycheck_to_paycheck"} <= set(formulas)
    props = formulas["forecast_liquid_summary"]["args_schema"]["properties"]
    assert {"horizon_months", "paths", "cushion", "seed", "changes", "with_backtest"} <= set(props)
    assert formulas["forecast_liquid"]["category"] == "forecast" and formulas["paycheck_to_paycheck"]["category"] == "health"
    assert all(f["description"] for f in formulas.values())
    assert "overdraft_risk" in formulas["forecast_liquid"]["result_schema"]["properties"]


def test_forecast_summary_through_the_formula_endpoint(client):
    r = client.post("/api/profiles/C001/formulas/forecast_liquid_summary", json={})
    assert r.status_code == 200 and "X-Compute-Ms" in r.headers
    body = strict(r)
    assert body["daily"] is None and body["monthly"] is None            # the summary version leaves the arrays out
    assert body["profile_id"] == "C001" and body["horizon_months"] == 24 and body["paths"] == 1000
    assert {"overdraft_risk", "cushion_risk", "low_point", "monthly_net", "drivers", "events", "assumptions", "backtest", "calibration"} <= set(body)
    direct = forecast_liquid_summary(load_profiles()["C001"])
    assert body["cushion_risk"]["probability"] == pytest.approx(direct.cushion_risk.probability)
    assert body["low_point"]["p50"] == pytest.approx(direct.low_point.p50)
    again = client.post("/api/profiles/C001/formulas/forecast_liquid_summary", json={"horizon_months": 24})   # same arguments after defaults
    assert again.headers["X-Cache"] == "hit" and strict(again) == body


def test_full_forecast_has_the_daily_and_monthly_arrays(client):
    body = strict(client.post("/api/profiles/C001/formulas/forecast_liquid", json={"horizon_months": 12}))
    assert len(body["daily"]["p50"]) == 91 and len(body["monthly"]) == 12


def test_bad_arguments_are_422(client):
    url = "/api/profiles/C001/formulas/forecast_liquid_summary"
    r = client.post(url, json={"horizon_months": "abc"})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["horizon_months"]
    assert client.post(url, json={"horizon_months": 500}).status_code == 422          # out of range
    assert client.post(url, json={"not_an_argument": 1}).status_code == 422           # unknown fields are rejected
    assert client.post(url, content="{not json", headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post("/api/profiles/C001/formulas/paycheck_to_paycheck", json={"x": 1}).status_code == 422


def test_unknown_formula_is_404(client):
    r = client.post("/api/profiles/C001/formulas/os.system", json={})
    assert r.status_code == 404 and r.json()["detail"] == "formula os.system not found"


def test_unimplemented_and_invalid_changes_are_reported_cleanly(client):
    url = "/api/profiles/C001/formulas/forecast_liquid_summary"
    r = client.post(url, json={"changes": [{"type": "add_loan", "amount": 10_000}]})
    assert r.status_code == 501 and r.json()["formula"] == "forecast_liquid_summary"
    r = client.post(url, json={"changes": [{"type": "lump_sum", "amount": -500, "day": "2020-01-01"}]})
    assert r.status_code == 422 and r.json()["formula"] == "forecast_liquid_summary"
    ok = client.post(url, json={"changes": [{"type": "lump_sum", "amount": -5000, "day": "2027-03-01"}]})
    assert ok.status_code == 200 and any(e["kind"] == "lump_sum" for e in strict(ok)["events"])


@pytest.mark.parametrize("pid", ["C001", "C002", "C003", "C004", "C005"])
def test_every_formula_and_profile_survives_a_strict_json_round_trip(client, pid):
    """Catches numpy / pandas / NaN serialization bugs: every response must parse as strict JSON."""
    assert strict(client.get(f"/api/profiles/{pid}"))["id"] == pid
    for f in client.get("/api/formulas").json():
        r = client.post(f"/api/profiles/{pid}/formulas/{f['name']}", json={})
        assert r.status_code == 200, (pid, f["name"], r.text[:200])
        body = strict(r)
        assert body and "X-Compute-Ms" in r.headers
        stack = [body]
        while stack:                                                   # nothing but plain JSON types, all numbers finite
            x = stack.pop()
            if isinstance(x, dict): stack += x.values()
            elif isinstance(x, list): stack += x
            else: assert x is None or isinstance(x, (str, bool, int)) or (isinstance(x, float) and math.isfinite(x))


# ------------------------------------------------------------------ job-loss stress test
def test_job_loss_stress_c002(client):
    body = strict(client.post("/api/profiles/C002/formulas/job_loss_stress", json={}))
    assert body["applicable"] and body["emergency_fund"]["months_covered"] < 3 and body["emergency_fund"]["gap"] > 0
    base, markov, lost = body["scenarios"]
    assert [base["name"], markov["name"], lost["name"]] == ["baseline", "markov", "lost_job_now"]
    assert base["overdraft_probability"] == 0 and lost["overdraft_probability"] == 1
    assert base["overdraft_probability"] <= markov["overdraft_probability"] <= lost["overdraft_probability"]
    assert lost["first_overdraft_p50"] is not None and lost["months_until_overdraft_p50"] < 6


def test_job_loss_stress_is_not_applicable_to_retirees(client):
    retired = next(p for p in load_profiles().values() if p.employment == "retired")
    body = strict(client.post(f"/api/profiles/{retired.id}/formulas/job_loss_stress", json={}))
    assert body["applicable"] is False and body["scenarios"] == [] and body["emergency_fund"]["essential_monthly"] > 0
