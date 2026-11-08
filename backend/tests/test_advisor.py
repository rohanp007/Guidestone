"""The advisor (docs/rules.md section 4) with the model mocked. The rule formulas and the grounding checks are real."""
from __future__ import annotations

import asyncio
import re

import pytest

from app.agent import advisor, cache, orchestrator
from app.agent.advisor import SPECS, AdvisorAnswer, AdvisorMove
from app.data.loader import load_profiles
from app.formulas import forecast as fc
from app.llm import client, grounding
from app.llm.client import LLMResponseError, LLMResult, LLMUnavailable
from app.llm.facts import forecast_facts

GOLDEN = ["C002", "C067", "C095", "C010", "C059"]
ALL_CODES = {"build_starter_cushion", "confirm_401k_match", "pay_card_high_apr", "pay_loan_high_apr", "build_full_cushion", "increase_retirement",
             "start_education_savings", "start_credit_building", "move_idle_cash_to_hys", "cd_renewal_review", "use_extra_paycheck",
             "recently_freed_payment", "automate_savings", "close_monthly_gap"}


@pytest.fixture(scope="module")
def profiles():
    return load_profiles()


@pytest.fixture(scope="module")
def inputs(profiles):
    """(Inputs, forecast facts) per golden persona, computed once."""
    out = {}
    for cid in GOLDEN:
        facts = forecast_facts(fc.forecast_liquid(profiles[cid], horizon_months=24))
        facts["employment"] = profiles[cid].employment
        out[cid] = (advisor.prepare(profiles[cid], facts), facts)
    return out


class Model:
    """Stands in for client.complete for the advisor. `answers` are returned in turn (the last one repeats); an exception is raised."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def __call__(self, agent, messages, tools=None, json_schema=None, **kw):
        assert agent == "advisor" and json_schema is AdvisorAnswer
        self.calls.append({"messages": messages, **kw})
        a = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(a, BaseException):
            raise a
        return LLMResult("{}", parsed=a(messages) if callable(a) else a)


def move(code, why=None, title="A title"):
    return AdvisorMove(move_code=code, title_text=title, why_text=why if why is not None else "")


def examples(messages) -> AdvisorAnswer:
    view = messages[1]["content"]
    return AdvisorAnswer(moves=[move(c, e) for c, e in zip(re.findall(r"move_code (\w+):", view), re.findall(r"example sentence: (.*)", view))])


def run(inp, facts, model, monkeypatch):
    monkeypatch.setattr(client, "complete", model)
    return asyncio.run(advisor.advise(inp, facts))


def example_of(inp, code):
    s = SPECS[code]
    return s.why(inp.candidates[code].facts) if callable(s.why) else s.why


def assert_grounded(advice, inp):
    """Every move is a candidate; each text, with its placeholders filled in, passes every grounding check against that move's own facts."""
    assert len(advice.moves) <= 3 and [m.rank for m in advice.moves] == list(range(1, len(advice.moves) + 1))
    assert len({m.move_code for m in advice.moves}) == len(advice.moves)
    for m in advice.moves:
        assert m.move_code in inp.candidates
        mt = advisor.move_text(m.move_code, inp.candidates[m.move_code].facts)
        report = grounding.check(f"{m.title_text}. {m.why_text}", {**mt.values, **inp.labels}, judgments=True, style=True, hints=False)
        assert report.ok, (m.move_code, [n.text for n in report.unmatched], [c.phrase for c in report.conflicts])


# ---- the catalogue ---------------------------------------------------------------------------------------------------------------

def test_every_candidate_move_has_a_spec():
    assert set(SPECS) == ALL_CODES


def test_placeholder_names_carry_their_kind_and_templates_use_only_their_own():
    for code, spec in SPECS.items():
        own = {p.name for p in spec.ph}
        texts = [spec.title] + [spec.why] if isinstance(spec.why, str) else [spec.title]
        if callable(spec.why):
            assert own, code
        for t in texts:
            used = set(re.findall(r"\{(\w+)\}", t))
            assert used <= own, (code, used - own)
    for p in (p for s in SPECS.values() for p in s.ph):
        kind = grounding._kind_of(p.name)
        if p.fmt is advisor.pct:
            assert kind == "ratio", p.name
        elif p.fmt is advisor.months:
            assert kind == "count", p.name
        elif p.fmt is advisor.usd or p.fmt is advisor.plain:
            assert kind == "money", p.name


def test_the_model_view_has_no_figures_and_no_verdict_word(inputs):
    for cid, (inp, facts) in inputs.items():
        view = advisor.model_view(inp, facts)
        body = re.sub(r"^\d+\. ", "", view, flags=re.M)
        assert not re.search(r"\$\d|\d%|(?<![_\w])\d{3,}(?![\w(])", body), cid                    # only placeholders reach the model
        assert advisor.VERDICT_TEXT[inp.verdict] in view and inp.verdict not in view
        assert "overall_status" not in view and "cushion_status" not in view


# ---- the five personas -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cid", GOLDEN)
def test_model_output_is_valid_grounded_and_figure_filled(cid, inputs, monkeypatch):
    inp, facts = inputs[cid]
    advice = run(inp, facts, Model(examples), monkeypatch)
    assert advice.verdict == inp.verdict and advice.source == "model" and not advice.grounding.retried
    assert advice.moves and all(m.source == "model" for m in advice.moves)
    assert not any("{" in m.why_text or "{" in m.title_text for m in advice.moves)                 # every placeholder filled
    assert_grounded(advice, inp)
    top = [m.move_code for m in advice.moves]
    assert top == [c for c in inp.candidates][:len(top)]                                          # waterfall order kept


@pytest.mark.parametrize("cid", GOLDEN)
def test_template_fallback_with_the_model_off(cid, inputs, monkeypatch):
    inp, facts = inputs[cid]
    model = Model(LLMUnavailable("down"))
    advice = run(inp, facts, model, monkeypatch)
    assert advice.source == "template" and all(m.source == "template" for m in advice.moves) and len(model.calls) == 1
    assert [m.move_code for m in advice.moves] == list(inp.candidates)[:3]                        # top 3 in waterfall order
    assert advice.verdict == inp.verdict
    assert_grounded(advice, inp)
    assert advice == advisor.template_advice(inp)                                                  # deterministic
    assert run(inp, facts, Model(LLMResponseError("junk")), monkeypatch).source == "template"


def test_verdicts_and_required_moves_of_the_personas(inputs):
    v = {cid: inputs[cid][0].verdict for cid in GOLDEN}
    assert v["C067"] == "act_now" and v["C059"] == "act_now" and v["C010"] == "plan_ahead"
    assert "pay_card_high_apr" in advisor.required(inputs["C067"][0]) and advisor.required(inputs["C010"][0]) == ["close_monthly_gap"]


# ---- validation and retry ------------------------------------------------------------------------------------------------------

def test_an_unknown_move_code_is_rejected_and_retried_at_temperature_0_4(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    bad = AdvisorAnswer(moves=[move("wire_money_abroad", "Do it."), *examples([None, {"content": ""}, {"content": advisor.model_view(inp, facts)}]).moves[:2]])
    model = Model(bad, examples)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and model.calls[1]["temperature"] == 0.4 and "temperature" not in model.calls[0]
    assert "wire_money_abroad" in model.calls[1]["messages"][-1]["content"] and advice.grounding.retried
    assert advice.source == "model" and "wire_money_abroad" not in {m.move_code for m in advice.moves}
    assert_grounded(advice, inp)


def test_unknown_codes_after_the_retry_fall_back_to_the_top_candidates(inputs, monkeypatch):
    inp, facts = inputs["C095"]
    advice = run(inp, facts, Model(AdvisorAnswer(moves=[move("nope", "x")])), monkeypatch)
    assert advice.source == "template" and [m.move_code for m in advice.moves] == list(inp.candidates)[:3]


def test_an_invented_figure_is_retried_then_that_move_gets_its_template(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    good = examples([None, {"content": advisor.model_view(inp, facts)}])
    lie = AdvisorAnswer(moves=[move("build_full_cushion", "You are only {cushion_gap} away from a cushion of $99,999.")] + good.moves[1:3])
    model = Model(lie)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and "$99,999" in model.calls[1]["messages"][-1]["content"]
    first = advice.moves[0]
    assert first.move_code == "build_full_cushion" and first.source == "template" and "99,999" not in first.why_text
    assert [m.source for m in advice.moves[1:]] == ["model"] * (len(advice.moves) - 1) and advice.source == "mixed"
    assert_grounded(advice, inp)


def test_a_retry_that_fixes_it_keeps_the_model_text(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    lie = AdvisorAnswer(moves=[move("build_full_cushion", "You have {cushion_now}, which is $99,999 too little.")])
    fixed = AdvisorAnswer(moves=[move("build_full_cushion", "You have {cushion_now} against a cushion of {cushion_target}.")])
    model = Model(lie, fixed)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and advice.source == "model" and advice.moves[0].why_text.startswith("You have $3,718 against a cushion of $13,167")


@pytest.mark.parametrize("why", ["Your balance is safe and nothing to worry about, so {cushion_gap} is optional.",
                                 "You have {cushion_gap} left to reach your target.  See cushion_status for details.",
                                 "Add {cushion_gap}; there is low risk either way."])
def test_judgments_and_machinery_are_rejected(inputs, monkeypatch, why):
    inp, facts = inputs["C002"]
    advice = run(inp, facts, Model(AdvisorAnswer(moves=[move("build_full_cushion", why)])), monkeypatch)
    assert advice.moves[0].source == "template"
    assert_grounded(advice, inp)


def test_a_move_that_uses_none_of_its_figures_is_rejected(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    model = Model(AdvisorAnswer(moves=[move("build_full_cushion", "Your cash is below the full cushion.")]))
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and "placeholders" in model.calls[1]["messages"][-1]["content"] and advice.moves[0].source == "template"


def test_figures_of_another_move_are_not_allowed(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    why = "You have {cushion_now} and a surplus of {monthly_surplus}."          # monthly_surplus belongs to automate_savings
    advice = run(inp, facts, Model(AdvisorAnswer(moves=[move("build_full_cushion", why)])), monkeypatch)
    assert advice.moves[0].source == "template"


def test_the_move_for_the_current_step_cannot_be_left_out(inputs, monkeypatch):
    inp, facts = inputs["C067"]                                            # step 3: pay_card_high_apr
    skip = AdvisorAnswer(moves=[move("increase_retirement", example_of(inp, "increase_retirement")),
                                move("automate_savings", example_of(inp, "automate_savings"))])
    model = Model(skip)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and "pay_card_high_apr" in model.calls[1]["messages"][-1]["content"]
    assert advice.moves[0].move_code == "pay_card_high_apr" and advice.moves[0].source == "template" and len(advice.moves) <= 3
    assert_grounded(advice, inp)


def test_at_most_three_moves_and_duplicates_are_dropped(inputs, monkeypatch):
    inp, facts = inputs["C059"]
    codes = list(inp.candidates)
    many = AdvisorAnswer(moves=[move(c, example_of(inp, c)) for c in [codes[0], codes[0], *codes]])
    advice = run(inp, facts, Model(many), monkeypatch)
    assert len(advice.moves) == 3 and len({m.move_code for m in advice.moves}) == 3
    assert_grounded(advice, inp)


def test_retirement_comes_before_education(inputs, monkeypatch):
    inp, facts = inputs["C059"]
    assert {"increase_retirement", "start_education_savings"} <= set(inp.candidates)
    wrong = AdvisorAnswer(moves=[move(c, example_of(inp, c)) for c in ("pay_loan_high_apr", "start_education_savings", "increase_retirement")])
    advice = run(inp, facts, Model(wrong), monkeypatch)
    codes = [m.move_code for m in advice.moves]
    assert codes.index("increase_retirement") < codes.index("start_education_savings")
    assert advice.notes == ["Retirement saving comes before education saving."]


def test_no_candidates_means_no_model_call(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    empty = advisor.Inputs(inp.waterfall, inp.moves, inp.verdict, {}, inp.labels)
    model = Model(examples)
    advice = run(empty, facts, model, monkeypatch)
    assert advice.moves == [] and model.calls == []


# ---- inside dashboard_open -----------------------------------------------------------------------------------------------------

def test_dashboard_open_carries_the_advice(profiles, monkeypatch):
    from test_orchestrator import FakeLLM
    cache.clear()
    fake = FakeLLM()
    monkeypatch.setattr(client, "complete", fake)
    r = asyncio.run(orchestrator.run("dashboard_open", profiles["C067"], {}))
    assert r.advice.verdict == "act_now" and r.advice.source == "model" and r.advice.moves[0].move_code == "pay_card_high_apr"
    assert {"priority_waterfall", "candidate_moves"} <= set(r.compute) and r.compute["candidate_moves"]["current_step"] == 3
    assert "advise" in [s.kind for s in r.trace] and {m["move_code"] for m in r.compute["candidate_moves"]["moves"]} >= {m.move_code for m in r.advice.moves}
    cache.clear()


def test_dashboard_open_with_the_advisor_off_still_returns_template_advice(profiles, monkeypatch):
    from test_orchestrator import FakeLLM
    cache.clear()
    fake = FakeLLM()
    fake.advice = LLMUnavailable("advisor down")
    monkeypatch.setattr(client, "complete", fake)
    r = asyncio.run(orchestrator.run("dashboard_open", profiles["C002"], {}))
    assert r.advice.source == "template" and r.degraded and r.summary and r.advice.moves
    cache.clear()
