"""Debts in scope, minimum payments and the amortization engine (app/formulas/debt.py). Arithmetic on small hand-made debts; the real
customers only for which debts count and what their minimums are."""
from __future__ import annotations

import pytest

from app.config import AS_OF_DATE
from app.data.loader import load_profiles
from app.formulas import debt as D
from app.formulas import forecast as fc


@pytest.fixture(scope="module")
def real():
    return load_profiles()


def loan(id="L", apr=0.12, balance=10000.0, payment=222.44) -> D.Debt:
    return D.Debt(id=id, kind="loan", apr=apr, balance=balance, fixed_min=payment)


def card(id="C", apr=0.24, balance=5000.0) -> D.Debt:
    return D.Debt(id=id, type="credit_card", kind="card", apr=apr, balance=balance, card_min_pct=0.01, card_min_floor=25.0)


# ---- the engine ------------------------------------------------------------------------------------------------------------------

def test_10000_at_12_percent_is_60_months():
    a = D.amortize([loan()], 0, ["L"])
    assert a.months_to_payoff == 60
    assert a.total_interest == pytest.approx(3346, abs=1)
    assert a.total_paid == pytest.approx(13346.4, abs=1)
    assert not a.never_pays_off and not a.hit_max_months
    assert a.payoff_date == fc.add_months(AS_OF_DATE, 60)             # counted from AS_OF_DATE, not today


def test_a_genuine_shortfall_is_not_rounded_away():
    a = D.amortize([loan(payment=222.0)], 0, ["L"])                  # 44 cents short each month: month 61 is needed
    assert a.months_to_payoff == 61


def test_payment_below_interest_never_pays_off():
    a = D.amortize([loan(payment=90.0)], 0, ["L"])                   # interest is $100 in the first month: the balance grows
    assert a.never_pays_off and a.months_to_payoff is None and a.payoff_date is None
    assert len(a.schedule) == a.max_months == 360
    assert a.per_debt["L"].never_pays_off
    assert a.schedule[-1].balances["L"] > 10000                      # the balance only grows


def test_payment_equal_to_interest_never_pays_off():
    assert D.amortize([loan(payment=100.0, balance=10000.0)], 0, ["L"], max_months=24).never_pays_off


def test_extra_money_can_rescue_a_stuck_debt():
    a = D.amortize([loan(payment=90.0)], 200.0, ["L"])
    assert a.months_to_payoff is not None and not a.never_pays_off


def test_a_capped_plan_that_does_cover_interest_is_not_never_pays_off():
    a = D.amortize([loan(payment=150.0, balance=50000.0, apr=0.03)], 0, ["L"], max_months=12)
    assert a.hit_max_months and not a.never_pays_off and a.months_to_payoff is None


def test_rollover_on_is_never_slower_and_here_faster():
    debts = [loan("small", 0.10, 1000.0, 200.0), loan("big", 0.20, 9000.0, 300.0)]
    on = D.amortize(debts, 50.0, ["big", "small"], rollover=True)
    off = D.amortize(debts, 50.0, ["big", "small"], rollover=False)
    assert on.months_to_payoff <= off.months_to_payoff
    assert on.months_to_payoff < off.months_to_payoff
    assert on.total_interest <= off.total_interest


def test_rollover_is_never_slower_across_many_shapes():
    for extra in (0, 25, 400):
        for apr2 in (0.05, 0.2, 0.3):
            debts = [loan("a", 0.08, 2500.0, 120.0), loan("b", apr2, 6000.0, 180.0), card("c", 0.22, 1800.0)]
            order = ["c", "b", "a"]
            on, off = (D.amortize(debts, extra, order, rollover=r) for r in (True, False))
            assert on.months_to_payoff <= off.months_to_payoff, (extra, apr2)


def test_extra_goes_to_the_first_unpaid_debt_and_spills_over():
    debts = [loan("first", 0.10, 300.0, 50.0), loan("second", 0.10, 5000.0, 100.0)]
    a = D.amortize(debts, 400.0, ["first", "second"])
    m1 = a.schedule[0]
    assert m1.balances["first"] == 0 and a.per_debt["first"].payoff_month == 1
    paid_second = m1.payments["second"]
    assert paid_second > 100.0                                       # the part of the extra the first debt could not take
    assert m1.total_payment == pytest.approx(sum(m1.payments.values()), abs=0.011)
    assert m1.total_payment == pytest.approx(50 + 100 + 400, abs=0.5)  # everything available was paid (the first debt's unused minimum rolled over)


def test_lump_sum_is_applied_at_month_0_before_interest():
    a = D.amortize([loan(payment=222.44)], 0, ["L"], lump_sum=4000.0)
    assert a.schedule[0].month == 0 and a.schedule[0].total_payment == 4000.0 and a.schedule[0].balances["L"] == 6000.0
    assert a.schedule[1].interest["L"] == pytest.approx(60.0)        # 1% of the reduced balance
    assert a.lump_applied == 4000.0


def test_lump_follows_lump_order_and_never_overpays():
    debts = [loan("a", 0.10, 1000.0, 50.0), loan("b", 0.20, 2000.0, 80.0)]
    a = D.amortize(debts, 0, ["b", "a"], lump_sum=1500.0, lump_order=["a", "b"])
    assert a.schedule[0].balances == {"a": 0.0, "b": 1500.0}
    big = D.amortize(debts, 0, ["b", "a"], lump_sum=10_000.0)
    assert big.lump_applied == 3000.0 and big.months_to_payoff == 0 and big.payoff_date == AS_OF_DATE


def test_card_minimum_is_the_floor_or_one_percent_plus_interest():
    c = card(balance=5000.0, apr=0.24)
    assert c.minimum(5000.0) == pytest.approx(0.01 * 5000 + 5000 * 0.02)      # 50 + 100
    assert c.minimum(300.0) == 25.0                                           # 3 + 6 = 9: the floor
    # it shrinks as the balance falls, so the first month pays more than the second
    a = D.amortize([c], 0, ["C"])
    assert a.schedule[0].payments["C"] > a.schedule[1].payments["C"]


def test_output_is_rounded_to_cents_only_there():
    a = D.amortize([loan(apr=0.0731, balance=1234.5678, payment=101.0)], 0, ["L"])
    for row in a.schedule:
        for v in (*row.payments.values(), *row.interest.values(), *row.balances.values(), row.total_payment):
            assert v == round(v, 2)
    assert a.total_interest == round(a.total_interest, 2)


def test_bad_ids_and_duplicates_are_errors():
    with pytest.raises(ValueError):
        D.amortize([loan()], 0, ["nope"])
    with pytest.raises(ValueError):
        D.amortize([loan()], 0, ["L"], lump_sum=10, lump_order=["nope"])
    with pytest.raises(ValueError):
        D.amortize([loan(), loan()], 0, ["L"])


def test_no_debts_is_an_empty_result():
    a = D.amortize([], 100.0, [])
    assert a.months_to_payoff == 0 and a.total_interest == 0 and a.schedule == []


# ---- debts in scope --------------------------------------------------------------------------------------------------------------

def test_pay_in_full_cards_and_mortgages_are_out_by_default(real):
    assert D.debts_in_scope(real["C010"]) == []                      # only a pay-in-full card
    assert D.debts_in_scope(real["C095"]) == []                      # pay-in-full card + mortgage
    assert D.excluded_mortgages(real["C095"], False) == ["A-C095-MORTGAGE"]
    inc = D.debts_in_scope(real["C095"], include_mortgage=True)
    assert [d.id for d in inc] == ["A-C095-MORTGAGE"] and D.excluded_mortgages(real["C095"], True) == []


def test_loan_minimum_is_the_observed_payment(real):
    (d,) = D.debts_in_scope(real["C059"])
    assert d.kind == "loan" and d.min_source == "observed" and d.fixed_min == pytest.approx(610.17)


def test_loan_without_a_recurring_payment_is_amortized_over_the_remaining_term(real):
    p = real["C059"].model_copy(update={"recurring": [r for r in real["C059"].recurring if r.kind != "loan_payment"]})
    (d,) = D.debts_in_scope(p)
    acct = p.accounts_of("auto_loan")[0]
    assert d.min_source == "amortized"
    assert d.fixed_min == pytest.approx(D.amortized_payment(acct.balance, acct.rate, acct.term_months_remaining))


def test_card_minimum_comes_from_the_assumptions(real):
    (d,) = D.debts_in_scope(real["C067"])
    assert d.kind == "card" and d.fixed_min is None and d.min_source == "formula"
    assert (d.card_min_pct, d.card_min_floor) == (0.01, 25.0)
    assert d.minimum(d.balance) == pytest.approx(0.01 * d.balance + d.balance * d.apr / 12)


def test_amortized_payment_matches_the_textbook():
    assert D.amortized_payment(10000, 0.12, 60) == pytest.approx(222.4445, abs=1e-3)
    assert D.amortized_payment(1200, 0.0, 12) == 100.0
