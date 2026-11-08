"""Scenario simulation (app/engine/simulate.py) and the forecast's `changes` hook (app/formulas/forecast.py)."""
from __future__ import annotations

import json
from datetime import timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from app.data.loader import load_profiles
from app.engine import simulate as sm
from app.formulas import debt as D
from app.formulas import forecast as fc
from app.formulas import payoff as P
from app.formulas.registry import FORMULAS
from app.llm.tools import session_tool

A = {k: v["value"] for k, v in fc.load_assumptions().items()}


@pytest.fixture(scope="module")
def real():
    return load_profiles()


def month1_cash(p, changes=None):
    s = fc.build_schedule(p, 6, A, changes)
    end = s.month_ends[0]
    return [e for e in s.cash if e[0] <= end]


# ---- forecast.py: changes=None is the old forecast -------------------------------------------------------------------------------

# Recorded from the forecast BEFORE the `changes` hook was added (paths=300, horizon 6, seed 0). The one band value that moved since is
# C059's month-3 p10 (12500.18 -> 12499.76): forecast.no_narrowing widened it by $0.42 so a band never narrows with the horizon.
GOLDEN = {"C067": (24542.552233, 20901.279805, 1890819.53804, 2), "C059": (15420.468734, 12499.759470, 961671.622452, 1)}


@pytest.mark.parametrize("pid", list(GOLDEN))
def test_forecast_without_changes_is_unchanged(real, pid):
    f = fc.forecast_liquid(real[pid], horizon_months=6, paths=300, with_backtest=False)
    end, p10, daily, events = GOLDEN[pid]
    assert f.end_balance.p50 == pytest.approx(end, abs=1e-5)
    assert f.monthly[2].p10 == pytest.approx(p10, abs=1e-5)
    assert sum(f.daily.p50) == pytest.approx(daily, abs=1e-4)
    assert len(f.events) == events


def test_empty_changes_and_none_are_identical(real):
    p = real["C067"]
    args = (p, 6, 300, 3000.0, 7, A)
    a, b = fc._simulate(*args, None), fc._simulate(*args, [])
    assert np.array_equal(a.pct, b.pct) and a.risks == b.risks
    assert fc.forecast_liquid(p, horizon_months=6, paths=300, with_backtest=False, changes=None) == \
        fc.forecast_liquid(p, horizon_months=6, paths=300, with_backtest=False, changes=[])


def test_a_flow_is_added_to_the_schedule_and_out_of_window_flows_are_dropped(real):
    p = real["C059"]
    day = p.as_of_date + timedelta(days=10)
    rows = fc.build_schedule(p, 3, A, [fc.Flow(day, -123.0, "x"), fc.Flow(p.as_of_date + timedelta(days=900), -5.0)]).cash
    assert [(e[0], e[2]) for e in rows if e[1] == "scenario"] == [(day, -123.0)]


# ---- the scenario model ----------------------------------------------------------------------------------------------------------

def test_scenario_is_strict():
    with pytest.raises(ValidationError):
        sm.Scenario(bogus=1)
    with pytest.raises(ValidationError):
        sm.Scenario(payoff_strategy="Z")
    with pytest.raises(ValidationError):
        sm.Scenario(expense_change={"monthly_delta": 5, "start_month": 4, "end_month": 2})
    with pytest.raises(ValidationError):
        sm.Scenario(add_loan={"principal": 1000, "apr": 0.05, "term_months": 12, "extra": 1})
    assert sm.Scenario().model_dump() == {"extra_monthly": None, "expense_change": None, "add_loan": None, "lump_sum": None, "payoff_strategy": None}


# ---- paired runs -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pid", ["C067", "C059", "C010"])
def test_a_no_op_scenario_differs_by_exactly_zero(real, pid):
    r = sm.simulate(real[pid], sm.Scenario())
    assert r.flows == 0 and r.plan is None
    assert r.diff.model_dump() == {k: 0 for k in sm.SimDiff.model_fields}
    assert r.baseline_monthly == r.scenario_monthly


def test_common_random_numbers_make_even_a_forced_rerun_identical(real):
    """Not the shortcut: run the scenario path with a zero flow and see that the draws really are shared."""
    p = real["C067"]
    base = sm.make_baseline(p)
    forced = sm._run(p, base, [fc.Flow(p.as_of_date + timedelta(days=3), 0.0, "noop")])
    assert forced.monthly == base.forecast.monthly
    assert sm.diff(sm.summarize(base.forecast, base.cushion), sm.summarize(forced, base.cushion)).model_dump() == {k: 0 for k in sm.SimDiff.model_fields}


def test_simulation_is_deterministic(real):
    s = sm.Scenario(expense_change={"monthly_delta": 150, "start_month": 2})
    assert sm.simulate(real["C059"], s) == sm.simulate(real["C059"], s)


def test_expense_change_moves_the_end_balance_by_the_flows(real):
    r = sm.simulate(real["C059"], sm.Scenario(expense_change={"monthly_delta": 200, "start_month": 1, "end_month": 2}))
    assert r.flows == 2 and r.diff.end_p50 == pytest.approx(-400, abs=1)
    assert r.diff.min_p10 < 0
    cut = sm.simulate(real["C059"], sm.Scenario(expense_change={"monthly_delta": -100}))
    assert cut.diff.end_p50 == pytest.approx(600, abs=1)                    # six months in the window


def test_new_loan_with_and_without_proceeds(real):
    p = real["C059"]
    loan = {"principal": 6000, "apr": 0.06, "term_months": 12, "start_month": 0}
    pay = D.amortized_payment(6000, 0.06, 12)
    cash = sm.simulate(p, sm.Scenario(add_loan={**loan, "disbursed_to_cash": True}))
    spent = sm.simulate(p, sm.Scenario(add_loan={**loan, "disbursed_to_cash": False}))
    assert cash.baseline_monthly[0].p50 == spent.baseline_monthly[0].p50
    assert cash.scenario_monthly[0].p50 - cash.baseline_monthly[0].p50 == pytest.approx(6000 - pay, abs=1)
    assert spent.scenario_monthly[0].p50 - spent.baseline_monthly[0].p50 == pytest.approx(-pay, abs=1)
    assert spent.diff.end_p50 == pytest.approx(-6 * pay, abs=1)


def test_payoff_fields_without_debt_change_nothing_and_say_so(real):
    r = sm.simulate(real["C010"], sm.Scenario(payoff_strategy="A", lump_sum=500))
    assert r.flows == 0 and r.diff.end_p50 == 0 and [w.code for w in r.warnings] == ["no_debt"]


def test_extra_money_alone_is_avalanche_and_lump_alone_is_c(real):
    p = real["C067"]
    assert sm.simulate(p, sm.Scenario(extra_monthly=200)).plan.code == "A"
    assert sm.simulate(p, sm.Scenario(lump_sum=1000)).plan.code == "C"
    assert sm.simulate(p, sm.Scenario(lump_sum=1000, extra_monthly=200)).plan.code == "D"
    named = sm.simulate(p, sm.Scenario(payoff_strategy="A")).plan
    assert named.extra_monthly == round(p.summary.monthly_surplus, 2)       # a named strategy: None = its default


# ---- no double payments ----------------------------------------------------------------------------------------------------------

def test_a_targeted_loan_payment_is_replaced_not_added(real):
    p = real["C059"]
    plan = P.plan_for_scenario(p, strategy="A", extra_monthly=None, lump_sum=None).amortization
    changes = sm.to_changes(p, sm.Scenario(), plan)
    base, scen = month1_cash(p), month1_cash(p, changes)
    loan_before = [e for e in base if e[1] == "debt_payment"]
    assert [round(e[2], 2) for e in loan_before] == [-610.17]
    assert not [e for e in scen if e[1] == "debt_payment"]                  # the recurring payment is gone...
    paid = [e for e in scen if e[1] == "payoff"]
    assert [round(e[2], 2) for e in paid if e[2]] == [-round(plan.schedule[0].payments["A-C059-AUTO_LOAN"], 2)]   # ...and the plan's is the only one
    # the month's total outflow differs from the baseline by exactly the extra money, nothing more
    out = lambda rows: sum(e[2] for e in rows if e[2] < 0)
    assert out(scen) - out(base) == pytest.approx(-p.summary.monthly_surplus, abs=0.01)
    assert len([e for e in scen if e[1] in ("debt_payment", "payoff") and e[2]]) == 1


def test_an_untargeted_debt_keeps_its_payment(real):
    """The mortgage is not in the plan unless asked for, so its recurring payment stays."""
    p = real["C095"]
    mort = [e for e in month1_cash(p) if e[1] == "debt_payment"]
    plan = P.payoff_strategies(p, include_mortgage=True).strategies["A"].amortization
    assert mort and not [e for e in month1_cash(p, sm.to_changes(p, sm.Scenario(), plan)) if e[1] == "debt_payment"]
    other = sm.to_changes(p, sm.Scenario(expense_change={"monthly_delta": 10}), None)
    assert [e for e in month1_cash(p, other) if e[1] == "debt_payment"] == mort


def test_a_lump_that_clears_a_debt_still_removes_its_recurring_payment(real):
    p = real["C059"]
    plan = P.plan_for_scenario(p, strategy="C", extra_monthly=None, lump_sum=None).amortization          # safe lump > the loan
    assert plan.months_to_payoff == 0 and all(r.month == 0 for r in plan.schedule)
    scen = month1_cash(p, sm.to_changes(p, sm.Scenario(), plan))
    assert not [e for e in scen if e[1] == "debt_payment"]
    assert [round(e[2], 2) for e in scen if e[1] == "payoff_lump"] == [-round(p.accounts_of("auto_loan")[0].balance, 2)]


def test_a_targeted_card_is_carried_by_the_flows_not_the_card_model(real):
    p = real["C067"]
    plan = P.plan_for_scenario(p, strategy="A", extra_monthly=None, lump_sum=None).amortization
    changes = sm.to_changes(p, sm.Scenario(), plan)
    base = fc._simulate(p, 6, 200, 3000.0, 1, A, None)
    scen = fc._simulate(p, 6, 200, 3000.0, 1, A, changes)
    interest = 14499.52 * 0.2484 / 12
    assert scen.month1_card < base.month1_card - 0.9 * interest                 # the card model now pays purchases only
    flow = sum(c.amount for c in changes if c.kind == "payoff" and c.day <= scen.schedule.month_ends[0])
    assert flow == pytest.approx(-plan.schedule[0].payments["A-C067-CREDIT_CARD"], abs=0.01)


def test_the_freed_payment_shows_after_payoff(real):
    p = real["C059"]
    r = sm.simulate(p, sm.Scenario(payoff_strategy="A"))
    assert r.plan.amortization.per_debt["A-C059-AUTO_LOAN"].payoff_month == 2
    gap = [s.p50 - b.p50 for b, s in zip(r.baseline_monthly, r.scenario_monthly)]
    assert gap[0] < -250                                                        # the extra payment month: much less cash than baseline
    # from month 3 on nothing is paid on the loan in either run: the paths move together, the gap stays put
    assert all(abs(gap[i] - gap[i - 1]) < 1e-6 for i in range(3, len(gap)))
    changes = sm.to_changes(p, sm.Scenario(), r.plan.amortization)
    last = max(c.day for c in changes if c.kind == "payoff" and c.amount)
    assert last <= fc.month_end(fc.add_months(p.as_of_date, 2))                 # no payment after the payoff month
    later = [e for e in fc.build_schedule(p, 6, A, changes).cash if e[0] > last and e[1] in ("debt_payment", "payoff")]
    assert later == []


def test_rollover_off_stops_the_freed_payment_instead_of_passing_it_on(real):
    ds = [D.Debt(id="a", kind="loan", apr=0.1, balance=500.0, fixed_min=300.0), D.Debt(id="b", kind="loan", apr=0.2, balance=6000.0, fixed_min=150.0)]
    on, off = (D.amortize(ds, 0.0, ["b", "a"], rollover=r) for r in (True, False))
    assert on.schedule[2].payments["b"] > off.schedule[2].payments["b"]
    assert off.schedule[2].payments["b"] == pytest.approx(150.0, abs=0.01)


# ---- payoff plans in the forecast ------------------------------------------------------------------------------------------------

def test_cushion_impact_uses_the_same_baseline_for_every_strategy(real):
    r = P.payoff_strategies(real["C067"])
    baselines = {s.cushion_impact.baseline.model_dump_json() for s in r.strategies.values()}
    assert len(baselines) == 1
    assert {s.cushion_impact.paths for s in r.strategies.values()} == {r.paths}
    for s in r.strategies.values():
        ci = s.cushion_impact
        assert ci.diff.min_p10 == pytest.approx(ci.scenario.min_p10 - ci.baseline.min_p10)


def test_simulate_scenario_result_shapes(real):
    full = sm.simulate_scenario(real["C067"], payoff_strategy="D")
    assert len(full.baseline_monthly) == len(full.scenario_monthly) == 6
    assert set(full.diff.model_dump()) == {"end_p50", "min_p10", "overdraft_prob", "cushion_prob", "months_p10_below_cushion"}
    assert full.plan.amortization.schedule
    out = json.dumps(sm.simulate_scenario_summary(real["C067"], payoff_strategy="D").model_dump(mode="json"))
    for banned in ("schedule", "baseline_monthly", "scenario_monthly", "p90", "balances"):
        assert banned not in out
    assert len(out) < 3000


def test_simulate_payoff_and_registration(real):
    for name in ("simulate_scenario", "simulate_scenario_summary", "simulate_payoff", "simulate_payoff_summary"):
        assert name in FORMULAS
    assert FORMULAS["simulate_scenario"].args is sm.Scenario
    r = sm.simulate_payoff(real["C067"], strategy="D")
    assert r.strategy.code == "D" and r.simulation.scenario_monthly and r.simulation.diff.end_p50 < 0
    assert sm.simulate_payoff(real["C010"], strategy="A").no_debt
    tool = session_tool(real["C067"], "simulate_payoff_summary")
    out = tool.fn(strategy="A")
    assert out.strategy.code == "A" and out.simulation.flows > 0
    assert session_tool(real["C067"], "simulate_scenario_summary").fn(expense_change={"monthly_delta": 50}).flows == 6
    with pytest.raises(ValueError):
        session_tool(real["C067"], "simulate_scenario")


# ---- bands never narrow with the horizon ---------------------------------------------------------------------------------------------

def test_no_narrowing_keeps_the_median_and_the_shape_and_only_widens():
    import numpy as np
    p50 = np.array([100.0, 100, 100, 100, 100, 100])
    p10 = np.array([100.0, 60, 20, 70, 0, 100])           # width 0, 40, 80, 30 (narrower), 100, 0 (zero width: a day with no uncertainty)
    p90 = np.array([100.0, 120, 150, 110, 130, 100])
    lo, hi = fc.no_narrowing(p50, p10, p90)
    widths = hi - lo
    assert (np.diff(widths) >= -1e-9).all() and np.allclose(widths, np.maximum.accumulate(p90 - p10))
    assert (lo <= p10 + 1e-9).all() and (hi >= p90 - 1e-9).all()                     # only wider
    assert np.allclose(lo[[0, 1, 2, 4]], p10[[0, 1, 2, 4]]) and np.allclose(hi[[0, 1, 2, 4]], p90[[0, 1, 2, 4]])   # untouched where already the widest
    assert lo[5] < 100 < hi[5]                                                       # a zero-width day after a wide one is not left at zero width


def test_every_customers_forecast_bands_widen_or_hold_with_the_horizon(real):
    import numpy as np
    for pid in list(real)[:40]:
        f = fc.forecast_liquid(real[pid], horizon_months=12, paths=200, with_backtest=False)
        daily = np.array(f.daily.p90) - np.array(f.daily.p10)
        monthly = np.array([m.p90 - m.p10 for m in f.monthly])
        assert (np.diff(daily) >= -1e-6).all(), pid
        assert (np.diff(monthly) >= -1e-6).all(), pid


# ---- the cushion measures use each month's lowest daily balance -------------------------------------------------------------------------

def test_month_min_p10_is_the_lowest_daily_p10_in_each_month(real):
    p = real["C002"]                                       # paid on the last business day: month-end points sit just after payday
    base = sm.make_baseline(p)
    lf = base.forecast
    lows = sm.month_min_p10(lf)
    assert len(lows) == len(lf.monthly) == 6 and len(lf.daily.p10) >= 180
    prev = lf.as_of_date
    for m, low in zip(lf.monthly, lows):
        lo, hi = (prev - lf.as_of_date).days + 1, (m.day - lf.as_of_date).days
        assert low == min(lf.daily.p10[lo:hi + 1]) and low <= m.p10 + 1e-9               # never above the month-end point
        prev = m.day
    assert any(low < m.p10 - 100 for m, low in zip(lf.monthly, lows))                    # and well below it in some month
    s = sm.summarize(lf, base.cushion)
    assert s.min_p10 == min(lows) and s.months_p10_below_cushion == sum(low < base.cushion for low in lows)
    assert s.end_p50 == lf.monthly[-1].p50                                              # the end balance is still the month-end


def test_month_min_p10_falls_back_to_month_end_without_daily_bands(real):
    lf = fc.forecast_liquid(real["C002"], horizon_months=3, paths=100, with_backtest=False).model_copy(update={"daily": None})
    assert sm.month_min_p10(lf) == [m.p10 for m in lf.monthly]
