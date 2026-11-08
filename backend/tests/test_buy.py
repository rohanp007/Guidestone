"""Buy formulas: APR by credit tier, the three plans, the verdict and the recommended plan."""
from __future__ import annotations

import pytest

from app.data.loader import load_profiles
from app.formulas import buy
from app.formulas.registry import FORMULAS
from app.research import pack


@pytest.fixture(scope="module")
def profiles():
    return load_profiles()


@pytest.mark.parametrize("score,tier,apr", [(850, "super_prime", .0441), (781, "super_prime", .0441), (780, "prime", .0615), (712, "prime", .0615), (661, "prime", .0615),
                                           (660, "near_prime", .0971), (601, "near_prime", .0971), (600, "subprime", .1352), (533, "subprime", .1352),
                                           (501, "subprime", .1352), (500, "deep_subprime", .1611), (300, "deep_subprime", .1611)])
def test_apr_is_looked_up_by_score(score, tier, apr):
    got, row = pack.apr_for_score(score)
    assert row["tier"] == tier and got == pytest.approx(apr)
    assert pack.apr_for_score(score, "used")[0] > got


def test_C095_is_affordable_and_cash_is_recommended(profiles):
    r = buy.buy_plans_for(profiles["C095"], "honda_crv")
    assert r.price == 30_920 and r.credit_tier == "prime" and r.loan_apr == pytest.approx(0.0615)
    assert r.recommended == "cash" and r.recommended_reason == "cash_keeps_cushion" and r.verdict == "affordable" and r.payment_ok and r.cushion_holds
    cash, fin, mix = (r.plans[k] for k in ("cash", "finance", "mix"))
    assert cash.loan_amount == 0 and cash.monthly_payment == 0 and cash.total_interest == 0 and cash.cash_after == pytest.approx(67_902.28 - 30_920, abs=0.01)
    assert fin.loan_amount == 30_920 and fin.monthly_payment == pytest.approx(599.93, abs=0.01) and fin.total_interest == pytest.approx(5_075.75, abs=0.01)
    assert fin.total_cost == pytest.approx(30_920 + fin.total_interest) and fin.payment_share_of_gross == pytest.approx(599.93 / 14_958.33, abs=1e-4)
    assert mix.down_payment == 30_920 and mix.same_as_cash and mix.loan_amount == 0               # the safe lump ($49,968) covers the whole price


def test_C002_cannot_afford_it_now(profiles):
    r = buy.buy_plans_for(profiles["C002"], "honda_accord")
    assert r.credit_tier == "subprime" and r.loan_apr == pytest.approx(0.1352) and r.safe_lump_sum == 0
    assert r.recommended == "finance" and r.recommended_reason == "no_safe_cash" and r.verdict == "not_now" and not r.payment_ok and not r.cushion_holds
    assert r.plans["finance"].payment_share_of_gross > 0.10 and r.plans["cash"].cash_after < 0 < r.plans["cash"].cushion_shortfall
    assert r.plans["mix"].down_payment == 0 and not r.plans["mix"].same_as_cash


@pytest.mark.parametrize("pay,cush,v", [(True, True, "affordable"), (True, False, "stretch"), (False, True, "stretch"), (False, False, "not_now")])
def test_verdict(pay, cush, v):
    assert buy.verdict_for(pay, cush) == v


def test_recommendation_rules(profiles, monkeypatch):
    p = profiles["C095"]
    monkeypatch.setattr(pack, "price", lambda v: 60_000.0)                    # more than the safe lump: cash would break the cushion
    r = buy.buy_plans_for(p, "honda_crv")
    assert r.recommended == "mix" and r.recommended_reason == "cash_would_break_cushion" and r.plans["mix"].down_payment == r.safe_lump_sum
    assert r.plans["mix"].loan_amount == pytest.approx(60_000 - r.safe_lump_sum, abs=0.01) and not r.plans["cash"].cushion_holds
    monkeypatch.setattr(pack, "price", lambda v: 30_920.0)
    monkeypatch.setattr(pack, "apr_for_score", lambda s, c="new": (0.02, pack.load()["auto_apr_by_tier"][0]))      # the loan is cheaper than savings earn
    r = buy.buy_plans_for(p, "honda_crv")
    assert r.recommended == "finance" and r.recommended_reason == "loan_cheaper_than_savings"


def test_a_car_without_a_cited_price_cannot_be_planned(profiles):
    with pytest.raises(ValueError):
        buy.buy_plans_for(profiles["C095"], "toyota_rav4")
    with pytest.raises(ValueError):
        buy.buy_plans_for(profiles["C095"], "nope")


def test_registered_and_serializable(profiles):
    assert "buy_plans" in FORMULAS
    out = FORMULAS["buy_plans"].fn(profiles["C095"], vehicle_id="mazda_cx5")
    assert out.model_dump_json() and out.plans["finance"].term_months == 60
