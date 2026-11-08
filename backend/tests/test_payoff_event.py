"""The simulate_payoff event ("should I pay it off?") with the model mocked. The payoff formulas, rules and grounding checks are real."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from app.agent import cache, orchestrator, payoff_advisor
from app.agent.events import EVENTS
from app.agent.payoff_advisor import AnswerModel
from app.data.loader import load_profiles
from app.llm import client
from app.llm.client import LLMResponseError, LLMResult, LLMUnavailable
from helpers import session_for


@pytest.fixture(scope="module")
def profiles():
    return load_profiles()


class Model:
    """Stands in for client.complete. `answers`: strings or callables(messages) -> string (the last repeats), or an exception to raise."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def __call__(self, agent, messages, tools=None, json_schema=None, **kw):
        assert agent == "advisor" and json_schema is AnswerModel
        self.calls.append({"messages": messages, **kw})
        a = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(a, BaseException):
            raise a
        return LLMResult("{}", parsed=AnswerModel(answer=a(messages) if callable(a) else a))


def example(messages) -> str:
    """A well-behaved model: adapt the example sentence it was shown (here, copy it)."""
    return re.search(r"Example \(adapt it, keeping the braces\): (.*)", messages[1]["content"]).group(1)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cache.clear()
    monkeypatch.setattr(orchestrator, "RERUN_DELAY_S", 60.0)                  # a transient result's background rerun never fires inside a test
    yield
    cache.clear()


def go(profile, model, params=None, **kw):
    client_complete = model
    import app.llm.client as c
    c.complete = client_complete
    return asyncio.run(orchestrator.run("simulate_payoff", profile, params or {}, **kw))


@pytest.fixture(autouse=True)
def _restore_client():
    real = client.complete
    yield
    client.complete = real


def codes(r):
    return r.payoff.answer.diagnostics.first_reasons + r.payoff.answer.diagnostics.final_reasons


# ---- the registered event -----------------------------------------------------------------------------------------------------------

def test_the_event_is_registered_with_its_params_and_a_fixed_plan():
    spec = EVENTS["simulate_payoff"]
    assert set(spec.params.model_fields) == {"extra_monthly", "lump_sum", "strategy"} and spec.cache == "params" and spec.pipeline == "payoff"
    assert spec.params.model_validate({}).model_dump() == {"extra_monthly": None, "lump_sum": None, "strategy": None}
    for bad in ({"strategy": "F"}, {"strategy": "Z"}, {"extra_monthly": -1}, {"lump_sum": -5}, {"customer_id": "C067"}):
        with pytest.raises(Exception):
            spec.params.model_validate(bad)


# ---- C067: a lump sum and extra money, with a cash tradeoff ---------------------------------------------------------------------

def test_C067_recommends_D_and_the_answer_carries_the_tradeoff(profiles):
    m = Model(example)
    r = go(profiles["C067"], m)
    p = r.payoff
    assert p.recommended.code == "D" and p.strategy_code == "D" and p.verdict == "pay_with_tradeoff"           # all set by code
    assert [c.code for c in p.comparison] == ["A", "B", "C", "D", "E"] and p.simulation.strategy.code == "D"
    assert p.simulation.simulation.baseline_monthly                                                                # the chart bands are there
    d = next(c for c in p.comparison if c.code == "D")
    assert "tradeoff_low_cushion" in {w.code for w in d.warnings}
    a = p.answer
    assert a.source == "model" and a.diagnostics.first_try_ok and len(m.calls) == 1 and not r.degraded
    assert "6 months" in a.text and "$28,594" in a.text                                                         # months and interest saved
    low = next(w for w in d.warnings if w.code == "tradeoff_low_cushion")
    assert f"${low.amount:,.0f}" in a.text and "cushion" in a.text and f"{d.months_p10_below_cushion} months" in a.text   # the tradeoff, in figures
    assert not re.search(r"[{}]", a.text) and r.status == "pay_with_tradeoff"
    assert [s.kind for s in r.trace if s.kind != "advise"] == ["precompute", "tool"] and r.trace[0].name == "payoff_strategies_summary"
    json.dumps(r.model_dump(mode="json"), allow_nan=False)


def test_the_model_sees_placeholders_and_no_figures_or_verdict(profiles):
    m = Model(example)
    go(profiles["C067"], m)
    view = m.calls[0]["messages"][1]["content"]
    body = view.split("Example (adapt it")[0]
    assert not re.search(r"\$\d|\d%|\d{2,}", body) and "pay_with_tradeoff" not in view and "tradeoff_low_cushion" not in view
    assert "{cushion_low_point}" in view and "{payoff_months}" in view and "Be sure to mention" in view


def test_C067_with_the_model_off_the_template_still_states_the_tradeoff(profiles):
    r = go(profiles["C067"], Model(LLMUnavailable("down")))
    a = r.payoff.answer
    assert a.source == "template" and a.diagnostics.first_reasons == ["llm_unavailable"] and r.degraded
    assert "debt-free in 6 months" in a.text and "$5,771" in a.text and "$10,511 cushion" in a.text and "6 months" in a.text
    assert r.payoff.verdict == "pay_with_tradeoff" and r.payoff.simulation is not None
    assert orchestrator.is_transient(r)                                                                          # a stand-in for the model: short TTL


# ---- C059: the lump sum is bigger than the debt --------------------------------------------------------------------------------------

def test_C059_lump_exceeds_debt(profiles):
    r = go(profiles["C059"], Model(example))
    p = r.payoff
    d = next(c for c in p.comparison if c.code == "D")
    w = next(w for w in d.warnings if w.code == "lump_exceeds_debt")
    assert p.verdict == "pay_it_off" and p.strategy_code == "D" and w.unused_amount > 0
    assert f"${w.unused_amount:,.0f}" in p.answer.text and "stays in cash" in p.answer.text
    assert p.answer.source == "model"
    off = go(profiles["C059"], Model(LLMUnavailable("down")), refresh=True).payoff.answer
    assert off.source == "template" and f"${w.unused_amount:,.0f} stays in cash" in off.text


# ---- C095 (and every customer with no debt) -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("cid", ["C095", "C002", "C010"])
def test_no_debt_is_deterministic_and_calls_no_model(profiles, cid):
    m = Model(example)
    r = go(profiles[cid], m)
    p = r.payoff
    assert m.calls == [] and p.verdict == "no_debt" and p.strategy_code is None and p.simulation is None and p.comparison == []
    assert p.answer.text == "You have no debt to pay off." and p.answer.source == "template" and p.answer.diagnostics.first_reasons == ["no_debt"]
    assert not r.degraded and not orchestrator.is_transient(r) and [s.kind for s in r.trace] == ["precompute"]


# ---- checks, retry, fallback ------------------------------------------------------------------------------------------------------

def test_an_invented_figure_is_retried_at_0_4_then_the_template_is_served(profiles):
    m = Model("You would be debt-free in {payoff_months}, saving $99,999 in interest.")
    r = go(profiles["C067"], m)
    a = r.payoff.answer
    assert len(m.calls) == 2 and m.calls[1]["temperature"] == 0.4 and "$99,999" not in a.text
    assert a.source == "template" and "figure_not_in_facts" in a.diagnostics.first_reasons and a.diagnostics.retried and r.degraded


def test_a_retry_that_fixes_the_answer_keeps_the_model_text(profiles):
    good = "{payoff_months} to be debt-free, saving {interest_saved}. Cash dips to {cushion_low_point} for {cushion_dip_months}."
    m = Model("It is safe to pay it off.", good)
    r = go(profiles["C067"], m)
    assert len(m.calls) == 2 and r.payoff.answer.source == "model" and r.payoff.answer.text.startswith("6 months to be debt-free")
    assert r.payoff.answer.diagnostics.first_reasons[0] in ("judgment", "no_placeholder") and not r.degraded


@pytest.mark.parametrize("text,reason", [
    ("Yes, you should pay it off, and it is safe: {payoff_months}, {interest_saved}, {cushion_low_point}.", "judgment"),
    ("Debt-free in {payoff_months}, saving {interest_saved}. Cash dips to {cushion_low_point}. It is fine. Do it.", "too_long"),
    ("You would save a lot and be debt-free soon.", "no_placeholder"),
    ("You would be debt-free in {payoff_months}, saving {interest_saved}.", "warning_not_mentioned"),
    ("See payoff_months and {interest_saved}, {cushion_low_point}.", "style"),
])
def test_each_check_rejects_with_a_reason_code(profiles, text, reason):
    m = Model(text)
    r = go(profiles["C067"], m)
    assert reason in r.payoff.answer.diagnostics.first_reasons, r.payoff.answer.diagnostics.first_reasons
    assert r.payoff.answer.source == "template" and len(m.calls) == 2
    assert all(re.fullmatch(r"[a-z_0-9]+", c) for c in codes(r))


def test_a_model_answer_that_is_not_json_falls_back(profiles):
    r = go(profiles["C067"], Model(LLMResponseError("junk")))
    assert r.payoff.answer.source == "template" and r.payoff.answer.diagnostics.first_reasons == ["llm_bad_reply"]


def test_past_the_budget_the_template_is_served(profiles, monkeypatch):
    async def slow(agent, messages, **kw):
        await asyncio.sleep(5)
    monkeypatch.setenv("ADVISOR_BUDGET_S", "0.05")
    r = go(profiles["C067"], slow)
    assert r.payoff.answer.source == "template" and r.payoff.answer.diagnostics.first_reasons == ["budget_exceeded"] and orchestrator.is_transient(r)


@pytest.mark.parametrize("cid", ["C067", "C059"])
def test_every_template_passes_the_same_checks(profiles, cid):
    r = go(profiles[cid], Model(LLMUnavailable("down")))
    p = r.payoff
    summary = r.compute["payoff_strategies_summary"]
    from app.formulas.payoff import PayoffStrategiesSummary
    s = PayoffStrategiesSummary.model_validate(summary)
    chosen = next(c for c in s.strategies if c.code == p.strategy_code)
    pf = payoff_advisor.facts_for(s, chosen)
    text, reasons, _ = payoff_advisor.check_answer(payoff_advisor.template_source(p.verdict, pf), pf, p.verdict)
    assert reasons == [] and text == p.answer.text


@pytest.mark.parametrize("verdict", ["keep_cash", "no_room", "no_debt"])
def test_the_fixed_sentences_are_clean(verdict):
    from app.llm import grounding
    t = payoff_advisor.template_text(verdict, None)
    assert not grounding.check_style(t, {}) and not grounding.check_wording(t, {}, judgments=True)


# ---- params, caching, the endpoint --------------------------------------------------------------------------------------------------

def test_params_change_the_plan_and_are_part_of_the_cache_key(profiles):
    m = Model(example)
    base = go(profiles["C067"], m)
    assert base.payoff.extra_monthly == pytest.approx(755.67, abs=0.01) and base.payoff.lump_sum == pytest.approx(10203.64, abs=0.01)
    again = go(profiles["C067"], m)
    assert orchestrator.is_cache_hit(again) and len(m.calls) == 1
    other = go(profiles["C067"], m, {"extra_monthly": 0, "lump_sum": 0})
    assert not orchestrator.is_cache_hit(other) and other.payoff.extra_monthly == 0 and other.payoff.lump_sum == 0
    assert other.payoff.recommended.code == "E" and other.payoff.verdict == "no_room"                         # nothing to pay with
    chosen = go(profiles["C067"], m, {"strategy": "A"})
    assert chosen.payoff.strategy_code == "A" and chosen.payoff.recommended.code == "D" and chosen.payoff.simulation.strategy.code == "A"
    assert go(profiles["C067"], m, {"strategy": "A"}).trace[-1].kind == "cache_hit"
    assert len({orchestrator.cache.cache_key("C067", "simulate_payoff", p, "v", "d") for p in ({}, {"lump_sum": 0}, {"lump_sum": 100}, {"strategy": "A"})}) == 4


def test_the_endpoint(profiles):
    client.complete = Model(example)
    c = session_for("C067")
    r = c.post("/api/me/agent/simulate_payoff", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event"] == "simulate_payoff" and body["customer_id"] == "C067" and body["status"] == "pay_with_tradeoff"
    p = body["payoff"]
    assert p["verdict"] == "pay_with_tradeoff" and p["recommended"]["code"] == "D" and [s["code"] for s in p["comparison"]] == list("ABCDE")
    assert p["simulation"]["simulation"]["scenario_monthly"] and p["answer"]["source"] == "model" and body["advice"] is None
    assert c.post("/api/me/agent/simulate_payoff", json={}).headers["X-Cache"] == "hit"
    assert c.post("/api/me/agent/simulate_payoff", json={"lump_sum": 500}).headers["X-Cache"] == "miss"
    assert c.post("/api/me/agent/simulate_payoff", json={"strategy": "F"}).status_code == 422
    assert c.post("/api/me/agent/simulate_payoff", json={"lump_sum": -1}).status_code == 422
    assert c.post("/api/me/agent/simulate_payoff", json={"customer_id": "C002"}).status_code == 422
    assert session_for("C095").post("/api/me/agent/simulate_payoff", json={}).json()["payoff"]["verdict"] == "no_debt"
