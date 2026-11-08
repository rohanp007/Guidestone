"""docs/rules.md: priority_waterfall, payoff_priority, candidate_moves. Section 5 (golden personas) on the real data; the rest on small
synthetic profiles. Thresholds are read from assumptions.json, as the formulas do."""
from __future__ import annotations

import math
from datetime import date

import pytest

from app.config import assumptions
from app.data.loader import load_profiles
from app.data.models import Account, Dependent, Profile, Recurring, RetirementInfo, SpendingProfile, Summary
from app.formulas import forecast as fc
from app.llm.facts import forecast_facts
from app.formulas import rules
from app.formulas.registry import FORMULAS
from helpers import session_for

T = {k: v["value"] for k, v in assumptions().items()}
AS_OF = date(2026, 10, 31)
SECTION_6 = ["starter_cushion", "high_apr_threshold", "cushion_months", "cushion_months_high", "retirement_target_pct", "card_low_cushion_apr",
             "low_cushion_months", "mortgage_extra_rate", "safe_savings_rate", "snowball_small_balance", "snowball_min_count",
             "idle_cash_months", "idle_checking_months", "cd_review_months", "credit_building_score"]


@pytest.fixture(scope="module")
def real():
    return load_profiles()


def moves_of(profile) -> set[str]:
    return {m.move_code for m in rules.candidate_moves(profile).moves}


# ---- synthetic profiles --------------------------------------------------------------------------------------------------------

def acct(id, type, balance, rate=0.0, **kw) -> Account:
    return Account(id=id, type=type, balance=balance, rate=rate, **kw)


def checking(balance) -> Account:
    return acct("CHK", "checking", balance)


def card(id, balance, rate, min_payment=100, **kw) -> Account:
    return acct(id, "credit_card", balance, rate, subtype="revolving", min_payment=min_payment, opening_balance=balance, credit_limit=50000, **kw)


def loan(id, balance, rate, min_payment=100, type="auto_loan", **kw) -> Account:
    return acct(id, type, balance, rate, min_payment=min_payment, opening_balance=balance, **kw)


def recurring(kind, monthly, **kw) -> Recurring:
    return Recurring(description=kind, category=kind, kind=kind, cadence="monthly", monthly_amount=monthly, amount_per_occurrence=monthly,
                     typical_day=1, months_seen=24, last_date=AS_OF, account_id="CHK", **kw)


def make(accounts, *, surplus=500.0, essentials=2000.0, employment="employed", gross=6000.0, retirement=None, dependents=(), credit=700,
         extra_recurring=(), ended=()) -> Profile:
    """A minimal valid customer. Essentials come from a housing bill; the waterfall's paycheck-to-paycheck check sees a healthy buffer."""
    liquid = sum(a.balance for a in accounts if a.type in ("checking", "savings"))
    return Profile(
        id="SYN", as_of_date=AS_OF, age=40, employment=employment, housing="renter", credit_score=credit, credit_tier="good",
        pay_frequency="monthly", gross_monthly=gross, net_monthly=4500.0, monthly_non_debt_expenses=essentials, accounts=accounts,
        recurring=[recurring("housing", -essentials), *extra_recurring], ended_recurring=list(ended),
        retirement=retirement or RetirementInfo(), dependents=[Dependent(age=a) for a in dependents],
        spending=SpendingProfile(seasonality={}, volatility={}, trend={}, monthly_variable_samples=[], card_share=0.0,
                                 carries_card_balance=False, card_carry_months=0, buffer_ratio_min=1.0, buffer_ratio_median=1.0),
        summary=Summary(liquid_cash=liquid, total_assets=liquid, total_debt=0, net_worth=liquid, net_monthly_income=4500.0,
                        monthly_non_debt_expenses=essentials, monthly_debt_payments=0, monthly_surplus=surplus, credit_utilization=None))


def done_through_5(*debts) -> Profile:
    """Steps 1-5 done: cash well above the cushion, retirement saving at the target (an IRA flow of 15% of gross)."""
    ira = recurring("investment_transfer", -T["retirement_target_pct"] * 6000.0)
    return make([checking(30000.0), *debts], extra_recurring=[ira])


def step(wf, n):
    return next(s for s in wf.steps if s.step == n)


# ---- the thresholds live in assumptions.json -----------------------------------------------------------------------------------

def test_every_section_6_threshold_is_in_assumptions_with_a_source():
    for key in SECTION_6 + ["dependent_age_limit", "freed_payment_days", "extra_paycheck_days", "promo_expiry_days"]:
        entry = assumptions()[key]
        assert isinstance(entry["value"], (int, float)) and len(entry["source"]) > 20 and entry["as_of"], key
    assert (T["high_apr_threshold"], T["card_low_cushion_apr"], T["mortgage_extra_rate"], T["retirement_target_pct"]) == (0.08, 0.20, 0.065, 0.15)
    assert (T["starter_cushion"], T["cushion_months"], T["cushion_months_high"], T["low_cushion_months"]) == (1000, 3, 6, 1)
    assert (T["snowball_small_balance"], T["snowball_min_count"], T["idle_cash_months"], T["idle_checking_months"],
            T["cd_review_months"], T["credit_building_score"]) == (2000, 3, 6, 2, 6, 620)


def test_formulas_read_thresholds_from_assumptions(monkeypatch):
    p = make([checking(1500.0)])
    assert step(rules.priority_waterfall(p), 1).status == "done"                      # 1,500 >= $1,000 starter cushion
    patched = {**T, "starter_cushion": 5000}                                          # essentials are 2,000, so the target is min(5000, 2000)
    monkeypatch.setattr(rules, "thresholds", lambda: patched)
    s1 = step(rules.priority_waterfall(p), 1)
    assert s1.target == 2000.0 and s1.status == "in_progress" and s1.gap == 500.0   # the patched threshold took effect: 1,500 < min(5,000, 2,000)
    monkeypatch.setattr(rules, "thresholds", lambda: {**T, "starter_cushion": 5000, "high_apr_threshold": 0.30})
    q = make([checking(1500.0), card("CARD", 5000, 0.24)])
    assert step(rules.priority_waterfall(q), 3).status == "done"                      # 24% is no longer "high" at a 30% threshold


def test_the_three_formulas_are_registered():
    assert {"priority_waterfall", "payoff_priority", "candidate_moves"} <= set(FORMULAS)
    assert set(FORMULAS["payoff_priority"].args.model_json_schema()["properties"]) == {"lump_sum", "extra_monthly", "use_cd"}


def test_endpoint_runs_the_rules():
    c = session_for("C002")
    wf = c.post("/api/profiles/C002/formulas/priority_waterfall", json={}).json()
    assert wf["current_step"] == 4 and len(wf["steps"]) == 7
    assert c.post("/api/profiles/C002/formulas/candidate_moves", json={}).json()["moves"]
    assert c.post("/api/profiles/C002/formulas/payoff_priority", json={"lump_sum": 100}).status_code == 200


# ---- section 5: golden personas ------------------------------------------------------------------------------------------------

def test_C002(real):
    wf = rules.priority_waterfall(real["C002"])
    assert wf.current_step == 4 and step(wf, 4).reason_code == "cushion" and step(wf, 4).detail["months"] == 6    # paycheck-to-paycheck
    assert {"build_full_cushion", "start_credit_building", "recently_freed_payment"} <= moves_of(real["C002"])


def test_C067(real):
    wf = rules.priority_waterfall(real["C067"])
    assert wf.current_step == 3 and step(wf, 3).reason_code == "high_apr" and step(wf, 2).status == "not_applicable"
    assert "pay_card_high_apr" in moves_of(real["C067"])


def test_C067_automate_savings(real):
    assert real["C067"].summary.monthly_surplus > 0
    assert "automate_savings" in moves_of(real["C067"])                 # transfers cover ~33% of the surplus, under the 50% minimum
    assert "automate_savings" in moves_of(real["C002"])


def test_C095(real):
    wf = rules.priority_waterfall(real["C095"])
    assert wf.current_step in (5, 7) and step(wf, 2).status == "unknown"
    assert {"move_idle_cash_to_hys", "cd_renewal_review", "increase_retirement"} <= moves_of(real["C095"])


def test_C010(real):
    wf = rules.priority_waterfall(real["C010"])
    assert step(wf, 2).status == "not_applicable" and step(wf, 5).status == "not_applicable"
    assert wf.current_step not in (1, 3)                                              # nothing that would make the verdict act_now
    assert all(s.monthly_suggested == 0 for s in wf.steps)                            # surplus <= 0
    moves = moves_of(real["C010"])
    assert "increase_retirement" not in moves and "cd_renewal_review" not in moves and "move_idle_cash_to_hys" not in moves   # conditions do not hold


def test_C059(real):
    wf = rules.priority_waterfall(real["C059"])
    assert wf.current_step == 3 and step(wf, 2).status == "unknown"
    assert {"recently_freed_payment", "pay_loan_high_apr"} <= moves_of(real["C059"])


def test_retirement_comes_before_education(real):
    moves = [m.move_code for m in rules.candidate_moves(real["C059"]).moves]
    assert moves.index("increase_retirement") < moves.index("start_education_savings")
    assert step(rules.priority_waterfall(real["C059"]), 5).step < step(rules.priority_waterfall(real["C059"]), 6).step


# ---- waterfall -----------------------------------------------------------------------------------------------------------------

def test_unknown_401k_does_not_block_the_order():
    p = make([checking(5000.0), card("CARD", 5000, 0.20)], surplus=500.0, retirement=RetirementInfo(has_401k=True, employer_match_pct=0.05))
    wf = rules.priority_waterfall(p)
    assert step(wf, 2).status == "unknown" and step(wf, 2).monthly_suggested == 0
    assert wf.current_step == 3 and step(wf, 3).monthly_suggested == 500.0            # the surplus went past the unknown step
    assert "confirm_401k_match" in moves_of(p)
    assert not [m for m in rules.candidate_moves(p).moves if m.move_code == "confirm_401k_match" and m.addresses_current_step]


def test_surplus_splits_across_steps():
    p = make([checking(400.0), card("CARD", 10000, 0.20)], surplus=1000.0)            # starter target 1,000: a $600 gap
    wf = rules.priority_waterfall(p)
    assert step(wf, 1).gap == 600.0 and step(wf, 1).monthly_suggested == 600.0
    assert step(wf, 3).monthly_suggested == 400.0 and wf.unallocated == 0
    assert sum(s.monthly_suggested for s in wf.steps) == 1000.0 and wf.current_step == 1

    q = make([checking(400.0)], surplus=1000.0)                                       # no debt: the remainder goes to the full cushion
    wf = rules.priority_waterfall(q)
    assert (step(wf, 1).monthly_suggested, step(wf, 4).monthly_suggested) == (600.0, 400.0)

    wf = rules.priority_waterfall(make([checking(400.0), card("CARD", 10000, 0.20)], surplus=-50.0))
    assert all(s.monthly_suggested == 0 for s in wf.steps) and wf.first_open_step == 1 and wf.current_step == "gap"   # no surplus: nothing to allocate


def test_cushion_is_six_months_with_young_dependents_or_a_paycheck_to_paycheck_flag():
    base = [checking(7000.0)]
    assert step(rules.priority_waterfall(make(base)), 4).status == "done"             # 3 x 2,000 = 6,000
    young = rules.priority_waterfall(make(base, dependents=[10]))
    assert step(young, 4).target == 12000.0 and step(young, 4).status == "in_progress"
    assert step(rules.priority_waterfall(make(base, dependents=[19])), 4).status == "done"      # not under 18


def test_pay_in_full_cards_are_not_debt():
    p = make([checking(5000.0), acct("CARD", "credit_card", 282, 0.2184, subtype="pay_in_full", min_payment=35, credit_limit=40000)])
    assert step(rules.priority_waterfall(p), 3).status == "done" and "pay_card_high_apr" not in moves_of(p)
    assert rules.payoff_priority(p).order == []


# ---- payoff priority -----------------------------------------------------------------------------------------------------------

def test_lump_sum_cushion_floor_and_the_20_percent_card_exception():
    p = make([checking(10000.0), loan("LOAN", 5000, 0.15)])                           # essentials 2,000
    out = rules.payoff_priority(p, lump_sum=9000)
    assert out.safe_lump_sum == 4000.0 and out.safe_lump_sum_low_cushion is None      # 10,000 - 3 x 2,000; no exception without a 20% card
    assert out.lump_sum_applied == 4000.0 and "tradeoff_low_cushion" not in out.flags
    q = make([checking(10000.0), card("CARD", 5000, 0.24)])
    out = rules.payoff_priority(q, lump_sum=9000)
    assert out.safe_lump_sum == 4000.0 and out.safe_lump_sum_low_cushion == 8000.0    # 10,000 - 1 x 2,000: both versions shown
    assert "tradeoff_low_cushion" in out.flags and out.lump_sum_applied == 8000.0
    assert rules.payoff_priority(q, lump_sum=1000).lump_sum_applied == 1000.0
    short = rules.payoff_priority(make([checking(5000.0), card("CARD", 5000, 0.24)]), lump_sum=500)
    assert short.safe_lump_sum == 0.0 and short.safe_lump_sum_low_cushion == 3000.0   # already under the 3-month floor
    just_under = make([checking(10000.0), card("CARD", 5000, 0.1999)])
    assert rules.payoff_priority(just_under).safe_lump_sum_low_cushion is None        # 19.99% is not 20%


def test_cards_come_before_loans_at_equal_apr():
    p = make([checking(5000.0), loan("A-LOAN", 3000, 0.18), card("Z-CARD", 3000, 0.18), loan("B-LOAN", 3000, 0.18, type="personal_loan")])
    assert [i.account_id for i in rules.payoff_priority(p).order] == ["Z-CARD", "A-LOAN", "B-LOAN"]
    q = make([checking(5000.0), card("CARD", 3000, 0.18), loan("LOAN", 3000, 0.22), card("LOW", 3000, 0.10)])
    assert [i.account_id for i in rules.payoff_priority(q).order] == ["LOAN", "CARD", "LOW"]      # highest APR first
    assert [i.reason_code for i in rules.payoff_priority(q).order] == ["high_apr"] * 3        # 10% is still at or above the 8% threshold


def test_debt_cheaper_than_savings_earn_gets_no_extra_payment():
    p = make([checking(5000.0), loan("CHEAP", 8000, 0.03), loan("MID", 8000, 0.05), loan("HIGH", 8000, 0.12)])
    out = rules.payoff_priority(p)
    assert [(i.account_id, i.reason_code) for i in out.order] == [("HIGH", "high_apr"), ("MID", "optional")]
    assert "keep_cash_earns_more" in out.flags


def test_mortgage_is_excluded_by_default():
    mortgage = lambda rate: loan("MORT", 200000, rate, min_payment=1500, type="mortgage")
    assert rules.payoff_priority(make([checking(500.0), mortgage(0.09)])).order == []            # 9%, but steps 1-5 are not done
    assert rules.payoff_priority(done_through_5(mortgage(0.06))).order == []                     # steps done, but 6% <= 6.5%
    out = rules.payoff_priority(done_through_5(mortgage(0.07)))
    assert [(i.account_id, i.reason_code) for i in out.order] == [("MORT", "mortgage_extra")]    # 7% > 6.5% and steps 1-5 done
    assert [(i.account_id, i.reason_code) for i in rules.payoff_priority(done_through_5(mortgage(0.09))).order] == [("MORT", "mortgage_extra")]
    assert "pay_loan_high_apr" not in moves_of(make([checking(500.0), mortgage(0.09)]))          # the mortgage is never a "loan to pay off" move
    assert step(rules.priority_waterfall(make([checking(500.0), mortgage(0.09)])), 3).status == "done"


def _interest(balance, apr, payment, months, lump=0.0):
    b, total = balance - lump, 0.0
    for _ in range(months):
        i = b * apr / 12
        total += i
        b = b + i - min(payment, b + i)
    return total


def test_cd_net_benefit_math():
    cd = acct("CD", "cd", 10000.0, 0.02, term_months_remaining=6, penalty_days=90, maturity_date=date(2027, 4, 30))
    p = make([checking(3000.0), card("CARD", 20000, 0.24, min_payment=400), cd])
    out = rules.payoff_priority(p, use_cd=True, extra_monthly=0)                   # (0 means no extra; None means use the surplus)
    opt = out.cd
    assert opt.penalty == round(90 * 10000 * 0.02 / 365, 2) == 49.32
    assert opt.interest_given_up == round(10000 * 0.02 * 6 / 12, 2) == 100.0
    expected_saved = _interest(20000, 0.24, 400, 6) - _interest(20000, 0.24, 400, 6, lump=10000)
    assert opt.interest_saved == pytest.approx(expected_saved, abs=0.2) and opt.interest_saved > 1000
    assert opt.net_benefit == pytest.approx(opt.interest_saved - opt.penalty - opt.interest_given_up, abs=0.02)
    assert opt.recommend_break is True and opt.remaining_months == 6
    assert rules.payoff_priority(p).cd is None                                        # only when the user selects the CD

    small = make([checking(3000.0), card("CARD", 600, 0.24, min_payment=300), acct("CD", "cd", 50000.0, 0.05, term_months_remaining=12, penalty_days=180)])
    bad = rules.payoff_priority(small, use_cd=True, extra_monthly=0).cd
    assert bad.net_benefit < 0 and bad.recommend_break is False                       # a small debt cannot repay breaking a big CD
    assert rules.payoff_priority(make([checking(3000.0)]), use_cd=True).cd is None    # no CD, no option


def test_snowball_extra_cost():
    big = card("BIG", 5000, 0.24, min_payment=150)
    small = [loan(f"S{i}", b, 0.09, min_payment=25, type="personal_loan") for i, b in ((1, 500), (2, 700), (3, 900))]
    p = make([checking(5000.0), big, *small])
    out = rules.payoff_priority(p, extra_monthly=300)
    assert [i.account_id for i in out.order][0] == "BIG"                              # avalanche: the 24% card first
    assert out.snowball is not None and out.avalanche is not None
    assert out.snowball_extra_cost == round(out.snowball.total_interest - out.avalanche.total_interest, 2) > 0
    fewer = rules.payoff_priority(make([checking(5000.0), big, *small[:2]]), extra_monthly=300)
    assert fewer.snowball_extra_cost is None and fewer.snowball is None              # only 2 small debts
    big_ones = rules.payoff_priority(make([checking(5000.0), big, *[loan(f"L{i}", 2500, 0.09, type="personal_loan") for i in range(3)]]), extra_monthly=300)
    assert big_ones.snowball_extra_cost is None                                       # not under $2,000


def test_freed_payments_roll_over():
    a = [loan("A", 1000, 0.20, min_payment=100, type="personal_loan"), loan("B", 8000, 0.10, min_payment=200, type="personal_loan")]
    months_rolling, _ = rules.simulate(a, a, 0, 0)
    months_flat = None
    b = a[1]                                                                          # B alone, without A's freed $100
    months_flat, _ = rules.simulate([b], [b], 0, 0)
    assert months_rolling < months_flat                                               # A's minimum joined B's once A was paid off


def test_flags():
    assert "no_debt" in rules.payoff_priority(make([checking(5000.0)])).flags
    assert "no_surplus" in rules.payoff_priority(make([checking(5000.0), card("C", 5000, 0.2)], surplus=0.0)).flags
    promo = acct("PROMO", "credit_card", 3000, 0.0, subtype="promotional", min_payment=50, opening_balance=3000, maturity_date=date(2026, 12, 15))
    assert "promo_rate_expiring" in rules.payoff_priority(make([checking(5000.0), promo])).flags
    assert "prepayment_penalty_assumed_none" in rules.payoff_priority(make([checking(5000.0), card("C", 5000, 0.2)])).flags


# ---- candidate moves -----------------------------------------------------------------------------------------------------------

def test_move_conditions():
    assert "start_credit_building" in moves_of(make([checking(5000.0)], credit=600))
    assert "start_credit_building" not in moves_of(make([checking(5000.0), card("C", 100, 0.2)], credit=600))   # has a card
    assert "start_credit_building" not in moves_of(make([checking(5000.0)], credit=620))                         # 620 is not below 620
    assert "move_idle_cash_to_hys" in moves_of(make([checking(4500.0)]))              # checking > 2 x 2,000
    assert "move_idle_cash_to_hys" not in moves_of(make([checking(4000.0)]))
    assert "move_idle_cash_to_hys" in moves_of(make([checking(1000.0), acct("SAV", "savings", 11500.0)]))        # liquid > 6 x 2,000
    cd = lambda d: acct("CD", "cd", 5000.0, 0.02, maturity_date=d)
    assert "cd_renewal_review" in moves_of(make([checking(3000.0), cd(date(2027, 4, 15))]))                      # 5.5 months out
    assert "cd_renewal_review" not in moves_of(make([checking(3000.0), cd(date(2027, 6, 1))]))
    assert "automate_savings" in moves_of(make([checking(3000.0)], surplus=100.0))
    assert "automate_savings" not in moves_of(make([checking(3000.0)], surplus=0.0))
    assert "automate_savings" in moves_of(make([checking(3000.0)], extra_recurring=[recurring("savings_transfer", -100.0)]))      # 20% < 50%
    assert "automate_savings" not in moves_of(make([checking(3000.0)], extra_recurring=[recurring("savings_transfer", -250.0)]))  # 50% covered
    assert "build_starter_cushion" in moves_of(make([checking(300.0)]))
    assert "increase_retirement" not in moves_of(make([checking(5000.0)], employment="retired"))
    young = make([checking(30000.0)], dependents=[5], extra_recurring=[recurring("investment_transfer", -900.0)])
    assert "start_education_savings" in moves_of(young)
    ended = make([checking(3000.0)], ended=[recurring("loan_payment", -300.0).model_copy(update={"last_date": date(2026, 9, 1)})])
    assert "recently_freed_payment" in moves_of(ended)
    old = make([checking(3000.0)], ended=[recurring("loan_payment", -300.0).model_copy(update={"last_date": date(2026, 5, 1)})])
    assert "recently_freed_payment" not in moves_of(old)


def test_moves_are_in_waterfall_order_and_carry_facts():
    result = rules.candidate_moves(make([checking(300.0), card("CARD", 5000, 0.24)], credit=500))
    assert [m.priority for m in result.moves] == sorted(m.priority for m in result.moves)
    first = result.moves[0]
    assert first.move_code == "build_starter_cushion" and first.addresses_current_step and first.facts["gap"] == 700.0
    card_move = next(m for m in result.moves if m.move_code == "pay_card_high_apr")
    assert card_move.facts["accounts"][0]["apr"] == 0.24 and card_move.priority == 3 and not card_move.addresses_current_step


# ---- review round 2 ------------------------------------------------------------------------------------------------------------

def test_idle_cash_is_suppressed_while_high_apr_debt_has_cash_to_pay_it():
    debt = card("CARD", 5000, 0.25)
    rich = make([checking(15000.0), debt])                              # liquid 15,000 > 6 x 2,000; safe lump 9,000 > 0
    assert rules.payoff_priority(rich).safe_lump_sum > 0
    assert "move_idle_cash_to_hys" not in moves_of(rich) and "pay_card_high_apr" in moves_of(rich)
    assert "move_idle_cash_to_hys" in moves_of(make([checking(15000.0)]))           # no debt: the move stays
    tight = make([checking(4500.0), debt])                              # checking > 2 months, but no cash above the 3-month floor
    assert rules.payoff_priority(tight).safe_lump_sum == 0 and "move_idle_cash_to_hys" in moves_of(tight)


def test_extra_monthly_none_uses_surplus_and_zero_means_none():
    p = make([checking(5000.0), card("CARD", 5000, 0.25)], surplus=300.0)
    assert rules.payoff_priority(p).extra_monthly_applied == 300.0
    zero = rules.payoff_priority(p, extra_monthly=0)
    assert zero.extra_monthly_applied == 0 and zero.avalanche.total_interest > rules.payoff_priority(p).avalanche.total_interest
    assert rules.payoff_priority(p, extra_monthly=50).extra_monthly_applied == 50


def test_leftover_surplus_flows_to_step_7():
    p = make([checking(7000.0)], surplus=500.0, retirement=None)        # steps 1-4 done; retirement target is large
    wf = rules.priority_waterfall(make([checking(7000.0)], surplus=100000.0))
    assert wf.unallocated == 0 and step(wf, 7).status == "todo" and step(wf, 7).detail["kind"] == "extra_investing"
    assert step(wf, 7).monthly_suggested > 0
    assert sum(s.monthly_suggested for s in wf.steps) == wf.surplus


def test_retiree_with_investments_has_a_drawdown_backstop_and_a_negative_surplus_gets_close_monthly_gap():
    ira = acct("IRA", "ira", 500000.0)
    p = make([checking(2000.0), ira], surplus=-20.0, employment="retired")
    wf = rules.priority_waterfall(p)
    assert step(wf, 4).status == "done" and step(wf, 4).detail["covered_by_investments"]
    codes = [m.move_code for m in rules.candidate_moves(p).moves]
    assert codes == ["close_monthly_gap"]
    short = make([checking(2000.0), acct("IRA", "ira", 5000.0)], surplus=-20.0, employment="retired")      # investments too small to cover
    assert "build_full_cushion" in moves_of(short) and "close_monthly_gap" in moves_of(short)
    assert "close_monthly_gap" not in moves_of(make([checking(3000.0)], surplus=0.0))


def test_C010_moves(real):
    assert [m.move_code for m in rules.candidate_moves(real["C010"]).moves] == ["close_monthly_gap"]
    wf = rules.priority_waterfall(real["C010"])
    assert wf.current_step == "gap" and wf.first_open_step is None


def test_verdict_for_C010_is_plan_ahead(real):
    p = real["C010"]
    f = fc.forecast_liquid(p)
    status = forecast_facts(f)["overall_status"]
    assert rules.verdict(p, status, f.overdraft_risk.probability) == "plan_ahead"
    assert rules.verdict(p, "on_track", 0.0) == "plan_ahead"                  # negative surplus: never better than plan_ahead
    assert rules.verdict(p, "on_track", 0.05) == "act_now"                    # an overdraft is possible
    assert rules.verdict(p, "at_risk", 0.0) == "act_now"


def test_verdict_rules():
    thin = make([checking(2000.0), acct("IRA", "ira", 5000.0)], surplus=-20.0, employment="retired")
    assert rules.verdict(thin, "on_track") == "act_now"                       # investments cover far fewer than 12 months
    assert rules.verdict(make([checking(1500.0)], surplus=500.0), "on_track") == "plan_ahead"      # step 4 open
    assert rules.verdict(make([checking(400.0)], surplus=500.0), "on_track") == "act_now"          # step 1 open
    assert rules.verdict(make([checking(10000.0), card("C", 5000, 0.25)], surplus=500.0), "on_track") == "act_now"   # step 3
    steady = make([checking(7000.0)], surplus=500.0)                                  # steps 1-4 done, retirement (step 5) still open
    assert rules.priority_waterfall(steady).first_open_step == 5
    assert rules.verdict(steady, "on_track") == "plan_ahead"                          # step 5
    assert rules.verdict(steady, "watch") == "plan_ahead" and rules.verdict(steady, "at_risk") == "act_now"
    done = make([checking(7000.0)], surplus=500.0, retirement=RetirementInfo(has_401k=True, employer_match_pct=0.5))
    assert rules.priority_waterfall(done).first_open_step in (7, None)                # nothing earlier is open
    assert rules.verdict(done, "on_track") == "on_track" and rules.verdict(done, "watch") == "plan_ahead"


# ---- cash that actually builds up ---------------------------------------------------------------------------------------------------

def test_cash_build_is_the_surplus_less_retirement_transfers():
    plain = rules.candidate_moves(make([checking(200.0)], surplus=500.0))
    f = next(m for m in plain.moves if m.move_code == "build_starter_cushion").facts
    assert f["cash_build_monthly"] == 500.0 and f["surplus_after"] == [] and "retirement_saving_monthly" not in f
    ira = make([checking(200.0)], surplus=500.0, extra_recurring=[recurring("investment_transfer", -300.0)])
    f = next(m for m in rules.candidate_moves(ira).moves if m.move_code == "build_starter_cushion").facts
    assert f["cash_build_monthly"] == 200.0 and f["retirement_saving_monthly"] == 300.0 and f["months_to_target"] == math.ceil(f["months_to_target"])
    assert f["months_to_target"] == math.ceil(800.0 / 200.0)                                  # a $1,000 starter fund, $200 in cash, $200 a month
    all_ira = make([checking(200.0)], surplus=300.0, extra_recurring=[recurring("investment_transfer", -300.0)])
    f = next(m for m in rules.candidate_moves(all_ira).moves if m.move_code == "build_starter_cushion").facts
    assert "months_to_target" not in f                                                         # nothing builds up in cash


def test_the_cushion_and_the_forecast_count_the_same_cash(real):
    for pid in list(real)[:30]:
        p = real[pid]
        f = fc.forecast_liquid(p, horizon_months=1, paths=100, with_backtest=False)
        assert rules._liquid(p) == pytest.approx(f.start_liquid), pid                        # checking + savings, both


def test_C002_surplus_reconciles_with_the_forecast_to_the_dollar(real):
    """Surplus 875.36 = income 3,637.00 - bills 1,478.15 - variable spending 1,283.50. The forecast also pays the $300 IRA transfer,
    spends a seasonal / inflated 1,296.2 on variable costs (12.7 more), bills 0.85 higher, and earns 2.0 interest."""
    p = real["C002"]
    s = p.summary
    bills = -sum(r.monthly_amount for r in p.recurring if r.kind in ("housing", "utility", "insurance", "subscription"))
    assert s.monthly_surplus == pytest.approx(s.net_monthly_income - bills - sum(p.variable_spend.values()), abs=0.01)
    assert s.monthly_debt_payments == 0                                                        # the $545 loan ended before as_of: already in the surplus
    f = fc.forecast_liquid(p, horizon_months=6, paths=200, with_backtest=False)
    month1 = sum(f.drivers.values())
    assert month1 == pytest.approx(s.monthly_surplus - 300 - 12.7 - 0.85 + 2.0, abs=1.0)
    assert f.drivers["investment_transfer"] == -300.0
    growth = (f.monthly[5].p50 - f.start_liquid) / 6
    assert 540 < growth < 600 and growth == pytest.approx(s.monthly_surplus - 300, abs=15)


def test_C002s_late_january_jump_is_a_payday(real):
    from datetime import date
    p = real["C002"]
    A = {k: v["value"] for k, v in assumptions().items()}
    sch = fc.build_schedule(p, 6, A, None)
    pay = [(d, a) for d, k, a, _, _ in sch.cash if k == "income" and date(2027, 1, 25) <= d <= date(2027, 2, 5)]
    assert pay == [(date(2027, 1, 29), pytest.approx(1818.5))]                               # semimonthly: 15th and the last business day
    assert not [e for e in sch.events if date(2027, 1, 25) <= e.day <= date(2027, 2, 5)]      # no extra-paycheck or other event


# ---- free surplus: retirement transfers are not allocated twice ----------------------------------------------------------------------

def ira(monthly):
    return [recurring("investment_transfer", -monthly)]


def test_the_waterfall_allocates_the_free_surplus_and_counts_transfers_as_step_5_progress():
    p = make([checking(7000.0)], surplus=875.0, extra_recurring=ira(300.0))          # steps 1-4 done (3 x 2,000 = 6,000)
    wf = rules.priority_waterfall(p)
    assert (wf.surplus, wf.retirement_transfers, wf.free_surplus) == (875.0, 300.0, 575.0)
    s5 = step(wf, 5)
    assert s5.current == 300.0 and s5.status == "in_progress" and s5.detail["ira_monthly"] == 300.0         # the transfer still counts as progress
    assert s5.monthly_suggested == pytest.approx(min(575.0, s5.gap)) and sum(s.monthly_suggested for s in wf.steps) + wf.unallocated == 575.0
    low = make([checking(1500.0)], surplus=875.0, extra_recurring=ira(300.0))        # step 4 open, gap 4,500: it takes the whole free surplus
    wf = rules.priority_waterfall(low)
    assert wf.current_step == 4 and step(wf, 4).monthly_suggested == 575.0 and step(wf, 5).monthly_suggested == 0
    none = rules.priority_waterfall(make([checking(1500.0)], surplus=875.0))
    assert step(none, 4).monthly_suggested == 875.0 and none.free_surplus == 875.0 and none.retirement_transfers == 0


def test_transfers_larger_than_the_surplus_leave_nothing_to_allocate():
    p = make([checking(1500.0)], surplus=200.0, extra_recurring=ira(300.0))
    wf = rules.priority_waterfall(p)
    assert wf.free_surplus == 0 and all(s.monthly_suggested == 0 for s in wf.steps) and wf.unallocated == 0
    assert wf.current_step == 4                                                      # a positive surplus: not the "gap" state
    f = next(m for m in rules.candidate_moves(p).moves if m.move_code == "build_full_cushion").facts
    assert "months_to_target" not in f
    assert "no_surplus" in rules.payoff_priority(p).flags


def test_payoff_extra_defaults_to_the_free_surplus():
    p = make([checking(5000.0), card("CARD", 5000, 0.25)], surplus=800.0, extra_recurring=ira(300.0))
    out = rules.payoff_priority(p)
    assert out.extra_monthly_applied == 500.0 and "no_surplus" not in out.flags
    assert rules.payoff_priority(p, extra_monthly=0).extra_monthly_applied == 0
    assert rules.payoff_priority(p, extra_monthly=700).extra_monthly_applied == 700           # the caller's own number is used as given


def test_C002_allocates_575_to_the_cushion(real):
    wf = rules.priority_waterfall(real["C002"])
    assert (wf.surplus, wf.retirement_transfers, wf.free_surplus) == (875.36, 300.0, 575.36)
    assert step(wf, 4).monthly_suggested == 575.36 and step(wf, 5).current == 300.0 and step(wf, 5).monthly_suggested == 0
