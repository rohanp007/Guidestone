"""Payoff strategies A-F (app/formulas/payoff.py) and the simulate_payoff / payoff_strategies registrations."""
from __future__ import annotations

import json
import time

import pytest

from app.data.loader import load_profiles
from app.data.models import Account
from app.formulas import debt as D
from app.formulas import forecast as fc
from app.formulas import payoff as P
from app.formulas import rules
from app.formulas.registry import FORMULAS
from app.llm.tools import session_tool
from helpers import session_for

CARD = "A-C067-CREDIT_CARD"
LOAN = "A-C067-PERSONAL_LOAN"


@pytest.fixture(scope="module")
def real():
    return load_profiles()


@pytest.fixture(scope="module")
def two_debts(real):
    """C067 with its closed personal loan reopened: a small 9% loan next to the big 24.84% card, so snowball and avalanche differ."""
    p = real["C067"]
    accounts = [a.model_copy(update={"balance": 1500.0, "opening_balance": 1500.0, "rate": 0.09, "min_payment": 150.0, "term_months_remaining": None,
                                     "maturity_date": None}) if a.id == LOAN else a for a in p.accounts]
    return p.model_copy(update={"accounts": accounts})


def debt(id, apr, balance, kind="loan") -> D.Debt:
    return D.Debt(id=id, kind=kind, apr=apr, balance=balance, fixed_min=50.0)


# ---- orders ----------------------------------------------------------------------------------------------------------------------

def test_avalanche_orders_by_apr_then_card_before_loan_then_smaller_balance():
    ds = [debt("loan_big", 0.20, 9000), debt("card", 0.20, 9000, "card"), debt("loan_small", 0.20, 500), debt("top", 0.30, 9999), debt("low", 0.05, 10)]
    assert P.avalanche_order(ds) == ["top", "card", "loan_small", "loan_big", "low"]


def test_snowball_orders_by_balance():
    ds = [debt("a", 0.30, 900), debt("b", 0.05, 100), debt("c", 0.10, 500)]
    assert P.snowball_order(ds) == ["b", "c", "a"]


# ---- strategies ------------------------------------------------------------------------------------------------------------------

def test_avalanche_never_costs_more_than_snowball(two_debts):
    r = P.payoff_strategies(two_debts)
    a, b = r.strategies["A"].amortization, r.strategies["B"].amortization
    assert r.strategies["A"].order[0] == CARD and r.strategies["B"].order[0] == LOAN
    assert a.total_interest <= b.total_interest
    assert r.snowball_extra_cost == r.strategies["B"].snowball_extra_cost == pytest.approx(b.total_interest - a.total_interest, abs=0.011)
    assert r.snowball_extra_cost >= 0
    assert r.strategies["A"].snowball_extra_cost is None


def test_minimum_only_is_the_baseline(real):
    r = P.payoff_strategies(real["C067"])
    e = r.strategies["E"]
    assert e.interest_saved_vs_minimum == 0 and e.months_saved_vs_minimum == 0
    assert e.extra_monthly == 0 and e.lump_sum == 0
    for c in "ABCD":
        assert r.strategies[c].interest_saved_vs_minimum == pytest.approx(e.amortization.total_interest - r.strategies[c].amortization.total_interest, abs=0.011)


def test_extra_zero_is_minimum_only_and_none_is_the_surplus(real):
    p = real["C067"]
    zero = P.payoff_strategies(p, extra_monthly=0)
    assert zero.strategies["A"].amortization.total_interest == zero.strategies["E"].amortization.total_interest
    assert zero.strategies["A"].amortization.schedule == zero.strategies["E"].amortization.schedule
    assert zero.strategies["A"].amortization.months_to_payoff == zero.strategies["E"].amortization.months_to_payoff
    default = P.payoff_strategies(p)
    assert default.extra_monthly == round(p.summary.monthly_surplus, 2) == default.strategies["A"].extra_monthly
    assert default.extra_monthly == rules.payoff_priority(p).extra_monthly_applied            # resolved the way payoff_priority does
    explicit = P.payoff_strategies(p, extra_monthly=default.extra_monthly)
    assert explicit.strategies["A"].amortization.total_interest == default.strategies["A"].amortization.total_interest


def test_lump_none_is_the_safe_lump_sum_and_a_bigger_one_warns(real):
    p = real["C067"]
    safe = rules.payoff_priority(p).safe_lump_sum
    r = P.payoff_strategies(p)
    assert r.lump_sum == r.safe_lump_sum == round(safe, 2) and r.strategies["D"].lump_sum == round(safe, 2)
    assert "exceeds_safe_lump" not in {w.code for w in r.strategies["D"].warnings}
    big = P.payoff_strategies(p, lump_sum=safe + 1500)
    w = {w.code: w for s in big.strategies.values() for w in s.warnings}["exceeds_safe_lump"]
    assert w.amount == pytest.approx(1500, abs=0.01)
    for c in "CD":
        assert "exceeds_safe_lump" in {w.code for w in big.strategies[c].warnings}
    for c in "ABE":                                                           # these never use the lump sum
        assert "exceeds_safe_lump" not in {w.code for w in big.strategies[c].warnings}
    assert big.strategies["D"].amortization.lump_applied == pytest.approx(safe + 1500, abs=0.01)   # still runs
    none = P.payoff_strategies(p, lump_sum=0)                                 # 0 = no lump sum: D is just A
    assert none.strategies["D"].amortization.total_interest == none.strategies["A"].amortization.total_interest


def test_custom_order_is_validated_against_the_debts_in_scope(two_debts):
    ok = P.payoff_strategies(two_debts, order=[LOAN, CARD])
    assert ok.strategies["F"].order == [LOAN, CARD] and ok.strategies["F"].name == "custom"
    assert P.payoff_strategies(two_debts, order=[LOAN]).strategies["F"].order == [LOAN, CARD]       # the rest follow
    for bad in (["nope"], [CARD, "nope"], [CARD, CARD]):
        with pytest.raises(ValueError):
            P.payoff_strategies(two_debts, order=bad)
    with pytest.raises(ValueError):
        P.payoff_strategies(two_debts, order=["A-C067-CHECKING"])             # a real account, but not a debt


def test_simulate_payoff_rejects_unknown_ids_and_strategies(real):
    from app.engine import simulate as sm
    with pytest.raises(ValueError):
        sm.simulate_payoff(real["C067"], strategy="F", order=["nope"])
    with pytest.raises(ValueError):
        P._plan(P.make_context(real["C067"]), "Z", None)


def test_mortgage_is_excluded_by_default_and_included_with_the_flag(real):
    p = real["C095"]
    assert P.payoff_strategies(p).debts == []
    r = P.payoff_strategies(p, include_mortgage=True)
    assert [d.id for d in r.debts] == ["A-C095-MORTGAGE"] and set(r.strategies) == set("ABCDE")
    assert r.strategies["A"].amortization.months_to_payoff < r.strategies["E"].amortization.months_to_payoff
    assert r.strategies["E"].amortization.total_interest > 100_000


def test_snowball_and_avalanche_agree_for_a_single_debt(real):
    r = P.payoff_strategies(real["C059"])
    assert r.strategies["A"].amortization == r.strategies["B"].amortization
    assert r.snowball_extra_cost == 0


# ---- the personas ----------------------------------------------------------------------------------------------------------------

def test_c067_single_card(real):
    p = real["C067"]
    started = time.perf_counter()
    r = P.payoff_strategies(p)
    assert time.perf_counter() - started < 1.5                                # the target for A-E
    assert r.paths == 1000 and r.runtime_ms < 1500
    assert [d.id for d in r.debts] == [CARD]
    a, b, d = (r.strategies[c] for c in "ABD")
    assert a.order == b.order == d.order == [CARD]
    assert d.amortization.total_interest < a.amortization.total_interest
    assert d.interest_saved_vs_minimum > a.interest_saved_vs_minimum
    # the recommendation is payoff_priority's, decided in code
    pp = rules.payoff_priority(p, lump_sum=r.lump_sum, extra_monthly=None)
    assert r.recommended.code == "D" and r.recommended.reason_code == pp.order[0].reason_code == "high_apr"
    assert [i.account_id for i in pp.order] == d.order
    assert not r.no_debt and not r.no_surplus and r.flags == rules.payoff_priority(p).flags
    # the safe lump sum leaves cash at the cushion floor, so the median sits there and the p10 band under it: the card exception
    # (APR >= 20%) lets a lump sum go down to one month of essentials, and the p10 stays above THAT floor
    ess = fc.essential_monthly(p)
    ci = d.cushion_impact
    assert ci.scenario.min_p10 >= 1 * ess
    assert "tradeoff_low_cushion" in r.flags
    assert ci.cushion == pytest.approx(3 * ess)
    assert ci.scenario.min_p10 < ci.baseline.min_p10                          # the lump sum costs cash
    assert ci.scenario.months_below_cushion > 0


def test_c059_auto_loan_pays_off_within_two_to_four_months(real):
    r = P.payoff_strategies(real["C059"])
    a = r.strategies["A"]
    assert 2 <= a.amortization.months_to_payoff <= 4
    assert a.months_saved_vs_minimum > 0 and a.interest_saved_vs_minimum > 0
    assert r.strategies["E"].amortization.months_to_payoff == 3
    assert r.recommended.code == "D"
    assert "lump_unused" in {w.code for w in r.strategies["D"].warnings}      # the safe lump sum is more than the loan


@pytest.mark.parametrize("pid", ["C095", "C010"])
def test_no_debt_personas_return_empty_strategies(real, pid):
    r = P.payoff_strategies(real[pid])
    assert r.no_debt and r.strategies == {} and r.comparison == [] and r.debts == []
    assert r.recommended.code is None and r.recommended.reason_code == "no_debt"
    assert r.no_surplus == ("no_surplus" in rules.payoff_priority(real[pid]).flags)
    assert P.payoff_strategies_summary(real[pid]).strategies == []


def test_no_surplus_mirrors_payoff_priority(real):
    assert P.payoff_strategies(real["C010"]).no_surplus is True
    assert P.payoff_strategies(real["C067"]).no_surplus is False


def test_no_surplus_with_debt_has_no_extra_money(two_debts):
    p = two_debts.model_copy(update={"summary": two_debts.summary.model_copy(update={"monthly_surplus": -50.0})})
    r = P.payoff_strategies(p, lump_sum=0)
    assert r.no_surplus and r.extra_monthly == 0
    assert r.strategies["A"].amortization.total_interest == r.strategies["E"].amortization.total_interest
    assert r.recommended.code == "E" and r.recommended.reason_code == "no_surplus"
    assert "no_surplus" in {w.code for w in r.strategies["A"].warnings}


# ---- comparison, summaries, registration -----------------------------------------------------------------------------------------

def test_comparison_rows(real):
    r = P.payoff_strategies(real["C067"])
    assert [c.code for c in r.comparison] == list("ABCDE")
    for row in r.comparison:
        s = r.strategies[row.code]
        assert (row.months, row.total_interest, row.interest_saved) == (s.amortization.months_to_payoff, s.amortization.total_interest, s.interest_saved_vs_minimum)
        assert row.cushion_min_p10 == s.cushion_impact.scenario.min_p10 and row.months_below_cushion == s.cushion_impact.scenario.months_below_cushion


def test_the_agent_summary_has_no_schedules_or_bands(real):
    s = P.payoff_strategies_summary(real["C067"])
    text = json.dumps(s.model_dump(mode="json"))
    for banned in ("schedule", "balances", "baseline_monthly", "scenario_monthly", "p90", "daily"):
        assert banned not in text
    assert {x.code for x in s.strategies} == set("ABCDE")
    assert len(text) < 6000


def test_registration_and_agent_tools(real):
    for name in ("payoff_strategies", "payoff_strategies_summary", "simulate_payoff", "simulate_payoff_summary"):
        assert name in FORMULAS
    assert set(FORMULAS["payoff_strategies"].args.model_fields) == {"extra_monthly", "lump_sum", "include_mortgage"}
    assert set(FORMULAS["simulate_payoff"].args.model_fields) == {"strategy", "extra_monthly", "lump_sum", "order"}
    tool = session_tool(real["C059"], "payoff_strategies_summary")
    assert tool.fn(extra_monthly=100).strategies[0].code == "A"
    with pytest.raises(ValueError):
        session_tool(real["C059"], "payoff_strategies")                       # agents get the summary variant


@pytest.mark.parametrize("pid", ["C067", "C059", "C095"])
def test_api_round_trip(pid):
    c = session_for(pid)
    for name, body in (("payoff_strategies", {}), ("simulate_payoff", {"strategy": "D"}), ("simulate_scenario", {"payoff_strategy": "A"}),
                       ("payoff_strategies_summary", {}), ("simulate_payoff_summary", {"strategy": "F", "order": []})):
        r = c.post(f"/api/profiles/{pid}/formulas/{name}", json=body)
        assert r.status_code == 200, (name, r.text[:200])
    bad = c.post(f"/api/profiles/{pid}/formulas/simulate_payoff", json={"strategy": "F", "order": ["nope"]})
    assert bad.status_code == 422 or pid == "C095"
