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
    assert r.plans["finance"].payment_share_of_gross > 0.10 and r.plans["cash"].status == "infeasible" and r.plans["cash"].cash_after is None
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


# ---- infeasible cash, overdraft warning, the high-APR-debt cap, the daily-low basis ----------------------------------------------------

def test_C067_cash_is_infeasible_and_never_recommended(profiles):
    r = buy.buy_plans_for(profiles["C067"], "honda_accord")
    cash, fin, mix = (r.plans[k] for k in ("cash", "finance", "mix"))
    assert cash.status == "infeasible" and [w.code for w in cash.warnings] == ["not_enough_cash"] and cash.warnings[0].amount == pytest.approx(28_395 - 20_714.98, abs=0.01)
    assert cash.cash_after is None and cash.cushion is None and cash.cushion_shortfall is None and cash.cushion_holds is None
    assert r.recommended == "mix" and r.recommended != "cash" and fin.status == mix.status == "ok"


def test_C067_step_three_is_open_so_cash_competes_with_the_card(profiles):
    r = buy.buy_plans_for(profiles["C067"], "honda_accord")
    assert r.high_apr_debt_open and r.high_apr_debt_apr == pytest.approx(0.2484)
    assert [w.code for w in r.plans["mix"].warnings] == ["competes_with_high_apr_debt"] and r.plans["mix"].warnings[0].amount == pytest.approx(0.2484)
    assert r.plans["finance"].warnings == []                                                 # no cash leaves: nothing competes
    assert r.verdict == "stretch"                                                              # the cushion already fails here, so the cap changes nothing


def test_an_open_step_three_caps_an_affordable_verdict_at_stretch(profiles, monkeypatch):
    r = buy.buy_plans_for(profiles["C095"], "honda_crv")
    assert r.verdict == "affordable" and not r.high_apr_debt_open and r.verdict_cap is None
    assert all(w.code != "competes_with_high_apr_debt" for pl in r.plans.values() for w in pl.warnings) and r.high_apr_debt_apr is None
    monkeypatch.setattr(buy, "_high_apr_debt", lambda p: (True, 0.2184))
    r = buy.buy_plans_for(profiles["C095"], "honda_crv")
    assert r.recommended == "cash" and r.verdict == "stretch" and r.verdict_cap == "competes_with_high_apr_debt" and r.high_apr_debt_apr == pytest.approx(0.2184)
    assert [w.code for w in r.plans["cash"].warnings] == ["competes_with_high_apr_debt"] and r.plans["finance"].warnings == []


def test_not_now_is_not_raised_by_the_cap(profiles, monkeypatch):
    monkeypatch.setattr(buy, "_high_apr_debt", lambda p: (True, 0.2))
    r = buy.buy_plans_for(profiles["C002"], "honda_accord")
    assert r.verdict == "not_now" and r.verdict_cap is None


def test_a_plan_whose_bad_luck_cash_goes_below_zero_warns_of_overdraft(profiles, monkeypatch):
    monkeypatch.setattr(pack, "price", lambda v: 80_000.0)
    r = buy.buy_plans_for(profiles["C002"], "honda_accord")
    seen = 0
    for pl in r.plans.values():
        codes = [w.code for w in pl.warnings]
        if pl.cushion is not None:
            assert ("overdraft_risk" in codes) == (pl.cushion.min_p10 < 0), pl.code
            seen += pl.cushion.min_p10 < 0
    assert seen >= 1 and r.plans["finance"].warnings[0].amount == pytest.approx(r.plans["finance"].cushion.min_p10, abs=0.01)
    ok = buy.buy_plans_for(profiles["C095"], "honda_crv")
    assert all(w.code != "overdraft_risk" for pl in ok.plans.values() for w in pl.warnings)


@pytest.mark.parametrize("pid,vid", [("C067", "honda_accord"), ("C095", "honda_crv"), ("C002", "honda_accord")])
def test_caption_and_below_cushion_facts_share_the_daily_low_basis(profiles, pid, vid):
    r = buy.buy_plans_for(profiles[pid], vid)
    chosen = r.plans[r.recommended]
    floor = chosen.cushion_floor
    lows = r.scenario_month_low_p10
    assert len(lows) == len(r.scenario_monthly) and len(r.baseline_month_low_p10) == len(r.baseline_monthly)
    assert sum(x < floor for x in lows) == chosen.cushion.months_p10_below_cushion                 # the same count, month by month
    assert min(lows) == pytest.approx(chosen.cushion.min_p10, abs=0.01) and chosen.cushion_shortfall == pytest.approx(max(0, floor - min(lows)), abs=0.01)
    assert buy._money(min(lows)) in r.caption and buy._money(min(r.baseline_month_low_p10)) in r.caption
    below = [m.day.strftime("%b") for m, x in zip(r.scenario_monthly, lows) if x < floor]
    assert (f"in {', '.join(below)}." in r.caption) if below else ("stays above" in r.caption)
