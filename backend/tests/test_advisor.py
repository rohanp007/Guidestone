"""The advisor (docs/rules.md section 4) with the model mocked. The rule formulas and the grounding checks are real."""
from __future__ import annotations

import asyncio
import dataclasses
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


def move(code, why=None):
    return AdvisorMove(move_code=code, why_text=why if why is not None else "")


def examples(messages) -> AdvisorAnswer:
    """A well-behaved model: the example sentence for the pinned move, then the first two other moves with theirs."""
    view = messages[1]["content"]
    codes, sentences = re.findall(r"move_code (\w+):", view), re.findall(r"example sentence[^:]*: (.*)", view)
    pinned = "The app's first move" in view
    return AdvisorAnswer(pinned_why=sentences[0] if pinned else None,
                         more=[move(c, e) for c, e in list(zip(codes, sentences))[1 if pinned else 0:][:2 if pinned else 3]])


def ans(pinned_why=None, *more):
    """AdvisorAnswer from a pinned sentence and (code, sentence) pairs."""
    return AdvisorAnswer(pinned_why=pinned_why, more=[move(c, w) for c, w in more])


def run(inp, facts, model, monkeypatch):
    monkeypatch.setattr(client, "complete", model)
    return asyncio.run(advisor.advise(inp, facts))


def example_of(inp, code):
    return advisor.sentence(code, inp.candidates[code].facts)


def assert_grounded(advice, inp):
    """Every move is a candidate; each text, with its placeholders filled in, passes every grounding check against that move's own facts."""
    assert len(advice.moves) <= 3 and [m.rank for m in advice.moves] == list(range(1, len(advice.moves) + 1))
    assert len({m.move_code for m in advice.moves}) == len(advice.moves)
    for m in advice.moves:
        assert m.move_code in inp.candidates
        mt = advisor.move_text(m.move_code, inp.candidates[m.move_code].facts)
        assert m.title_text == SPECS[m.move_code].title                                               # titles come from code
        assert not advisor.ZERO_FIGURE.search(m.why_text)                                             # no "$0" reads as broken
        report = grounding.check(m.why_text, {**mt.values, **inp.labels}, judgments=True, style=True, hints=False)
        assert report.ok, (m.move_code, [n.text for n in report.unmatched], [c.phrase for c in report.conflicts])


# ---- the catalogue ---------------------------------------------------------------------------------------------------------------

def test_every_candidate_move_has_a_spec():
    assert set(SPECS) == ALL_CODES


def test_placeholder_names_carry_their_kind_and_templates_use_only_their_own():
    for code, spec in SPECS.items():
        own = {p.name for p in spec.ph}
        texts = [spec.title] + ([spec.why] if isinstance(spec.why, str) else []) + [t for _, t in spec.variants]
        if callable(spec.why):
            assert own, code
        assert "{" not in spec.title and spec.title[0].isupper() and not spec.title.isupper(), code
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
    pinned = advisor.pinned_code(inp)
    assert pinned is None or advice.moves[0].move_code == pinned                                  # code pins the current step's move first
    assert len(advice.moves) <= 3


@pytest.mark.parametrize("cid", GOLDEN)
def test_template_fallback_with_the_model_off(cid, inputs, monkeypatch):
    inp, facts = inputs[cid]
    model = Model(LLMUnavailable("down"))
    advice = run(inp, facts, model, monkeypatch)
    pinned = advisor.pinned_code(inp)
    expected = ([pinned] if pinned else []) + [c for c in inp.candidates if c != pinned]
    assert advice.source == "template" and all(m.source == "template" for m in advice.moves) and len(model.calls) == 1
    assert [m.move_code for m in advice.moves] == expected[:3]                                    # pinned first, then waterfall order
    assert advice.verdict == inp.verdict
    assert_grounded(advice, inp)
    assert advice.moves == advisor.template_advice(inp).moves                                       # deterministic
    assert run(inp, facts, Model(LLMResponseError("junk")), monkeypatch).source == "template"


def test_verdicts_and_pinned_moves_of_the_personas(inputs):
    v = {cid: inputs[cid][0].verdict for cid in GOLDEN}
    assert v["C067"] == "act_now" and v["C059"] == "act_now" and v["C010"] == "plan_ahead"
    assert {cid: advisor.pinned_code(inputs[cid][0]) for cid in GOLDEN} == {
        "C002": "build_full_cushion", "C067": "pay_card_high_apr", "C095": "increase_retirement", "C010": "close_monthly_gap",
        "C059": "pay_loan_high_apr"}


# ---- the schema the server is asked to enforce ----------------------------------------------------------------------------------

def test_the_response_schema_limits_move_codes_to_this_customers_other_candidates(inputs):
    for cid in GOLDEN:
        inp, _ = inputs[cid]
        schema, pinned = advisor.response_schema(inp), advisor.pinned_code(inp)
        rest = [c for c in inp.candidates if c != pinned]
        assert schema["required"] == ["pinned_why"] and "pinned_why" in schema["properties"]
        if rest:
            more = schema["properties"]["more"]
            assert more["items"]["properties"]["move_code"]["enum"] == rest and more["maxItems"] == 2
            assert pinned not in rest and set(rest) <= set(inp.candidates)
        else:
            assert "more" not in schema["properties"]                                              # C010: nothing else to choose from
    assert advisor.response_schema(inputs["C002"][0])["properties"]["more"]["items"]["properties"]["move_code"]["enum"] == [
        "increase_retirement", "start_credit_building", "recently_freed_payment", "automate_savings"]


def test_the_schema_is_sent_and_a_rejecting_backend_falls_back_to_checking_here(inputs, monkeypatch):
    import httpx, json
    inp, facts = inputs["C067"]
    seen = []
    good = json.dumps({"pinned_why": example_of(inp, "pay_card_high_apr"), "more": []})

    def route(request):
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": good}}]})
    client.set_transport(httpx.MockTransport(route))
    try:
        advice = asyncio.run(advisor.advise(inp, facts, budget_s=10))
        assert "response_format" in seen[0] and seen[0]["response_format"]["type"] == "json_schema"
        enum = seen[0]["response_format"]["json_schema"]["schema"]["properties"]["more"]["items"]["properties"]["move_code"]["enum"]
        assert enum == ["build_full_cushion", "increase_retirement", "automate_savings"]
        assert "response_format" not in seen[1] and len(seen) == 2                                  # asked again without it
        assert advice.source == "model" and advice.moves[0].move_code == "pay_card_high_apr"
        assert client.TRACE[-1]["response_format"] == "rejected"
        asyncio.run(advisor.advise(inp, facts, budget_s=10))
        assert "response_format" not in seen[2]                                                       # remembered: not asked again
    finally:
        client.set_transport(None)


# ---- validation and retry ------------------------------------------------------------------------------------------------------

def test_an_unknown_move_code_is_rejected_and_retried_at_temperature_0_4(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    bad = ans(example_of(inp, "build_full_cushion"), ("wire_money_abroad", "Do it."), ("automate_savings", example_of(inp, "automate_savings")))
    model = Model(bad, examples)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and model.calls[1]["temperature"] == 0.4 and "temperature" not in model.calls[0]
    assert "wire_money_abroad" in model.calls[1]["messages"][-1]["content"] and advice.grounding.retried
    assert advice.source == "model" and "wire_money_abroad" not in {m.move_code for m in advice.moves}
    assert advice.diagnostics.first_reasons == ["unknown_move_code"]
    assert_grounded(advice, inp)


def test_unknown_codes_after_the_retry_keep_what_is_valid(inputs, monkeypatch):
    inp, facts = inputs["C095"]
    advice = run(inp, facts, Model(ans(example_of(inp, "increase_retirement"), ("nope", "x"))), monkeypatch)
    assert advice.moves[0].move_code == "increase_retirement" and "nope" not in {m.move_code for m in advice.moves}
    assert advice.diagnostics.final_reasons == ["unknown_move_code"]


def test_an_invented_figure_is_retried_then_that_move_gets_its_template(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    lie = ans("You are only {cushion_gap} away from a cushion of $99,999.", ("automate_savings", example_of(inp, "automate_savings")))
    model = Model(lie)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and "$99,999" in model.calls[1]["messages"][-1]["content"]
    first = advice.moves[0]
    assert first.move_code == "build_full_cushion" and first.source == "template" and "99,999" not in first.why_text
    assert [m.source for m in advice.moves[1:]] == ["model"] * (len(advice.moves) - 1) and advice.source == "mixed"
    assert_grounded(advice, inp)


def test_a_retry_that_fixes_it_keeps_the_model_text(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    lie = ans("You have {cushion_now}, which is $99,999 too little.")
    fixed = ans("You have {cushion_now} against a cushion of {cushion_target}.")
    model = Model(lie, fixed)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and advice.source == "model" and advice.moves[0].why_text.startswith("You have $3,718 against a cushion of $13,167")


@pytest.mark.parametrize("why", ["Your balance is safe and nothing to worry about, so {cushion_gap} is optional.",
                                 "You have {cushion_gap} left to reach your target.  See cushion_status for details.",
                                 "Add {cushion_gap}; there is low risk either way."])
def test_judgments_and_machinery_are_rejected(inputs, monkeypatch, why):
    inp, facts = inputs["C002"]
    advice = run(inp, facts, Model(ans(why)), monkeypatch)
    assert advice.moves[0].source == "template"
    assert_grounded(advice, inp)


def test_a_move_that_uses_none_of_its_figures_is_rejected(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    model = Model(ans("Your cash is below the full cushion."))
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and "placeholders" in model.calls[1]["messages"][-1]["content"] and advice.moves[0].source == "template"


def test_figures_of_another_move_are_not_allowed(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    why = "You have {cushion_now} and a surplus of {monthly_surplus}."          # monthly_surplus belongs to automate_savings
    advice = run(inp, facts, Model(ans(why)), monkeypatch)
    assert advice.moves[0].source == "template"


def test_the_pinned_move_is_first_even_if_the_model_gives_it_no_sentence(inputs, monkeypatch):
    inp, facts = inputs["C067"]                                            # step 3: pay_card_high_apr
    skip = ans(None, ("increase_retirement", example_of(inp, "increase_retirement")), ("automate_savings", example_of(inp, "automate_savings")))
    model = Model(skip)
    advice = run(inp, facts, model, monkeypatch)
    assert len(model.calls) == 2 and "pinned_why" in model.calls[1]["messages"][-1]["content"]
    assert advice.diagnostics.first_reasons == ["missing_required_move"]
    assert advice.moves[0].move_code == "pay_card_high_apr" and advice.moves[0].source == "template" and len(advice.moves) <= 3
    assert_grounded(advice, inp)


def test_the_model_cannot_move_or_repeat_the_pinned_move(inputs, monkeypatch):
    inp, facts = inputs["C067"]
    pc = example_of(inp, "pay_card_high_apr")
    advice = run(inp, facts, Model(ans(pc, ("pay_card_high_apr", pc), ("automate_savings", example_of(inp, "automate_savings")))), monkeypatch)
    assert [m.move_code for m in advice.moves] == ["pay_card_high_apr", "automate_savings"]


def test_at_most_two_more_after_the_pinned_move_and_duplicates_are_dropped(inputs, monkeypatch):
    inp, facts = inputs["C059"]
    pinned = advisor.pinned_code(inp)
    rest = [c for c in inp.candidates if c != pinned]
    many = ans(example_of(inp, pinned), *[(c, example_of(inp, c)) for c in [rest[0], rest[0], *rest]])
    advice = run(inp, facts, Model(many), monkeypatch)
    assert len(advice.moves) == 3 and len({m.move_code for m in advice.moves}) == 3 and advice.moves[0].move_code == pinned
    assert_grounded(advice, inp)


def test_retirement_comes_before_education(inputs, monkeypatch):
    inp, facts = inputs["C059"]
    assert {"increase_retirement", "start_education_savings"} <= set(inp.candidates)
    wrong = ans(example_of(inp, "pay_loan_high_apr"), ("start_education_savings", example_of(inp, "start_education_savings")),
                ("increase_retirement", example_of(inp, "increase_retirement")))
    advice = run(inp, facts, Model(wrong), monkeypatch)
    codes = [m.move_code for m in advice.moves]
    assert codes[0] == "pay_loan_high_apr" and codes.index("increase_retirement") < codes.index("start_education_savings")
    assert advice.notes == ["Retirement saving comes before education saving."]


def test_with_nothing_pinned_the_model_picks_up_to_three(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    flat = advisor.Inputs(inp.waterfall, inp.moves, inp.verdict,
                          {c: m.model_copy(update={'addresses_current_step': False}) for c, m in inp.candidates.items()}, inp.labels)
    assert advisor.pinned_code(flat) is None
    schema = advisor.response_schema(flat)
    assert "pinned_why" not in schema["properties"] and schema["required"] == ["more"] and schema["properties"]["more"]["maxItems"] == 3
    advice = run(flat, facts, Model(examples), monkeypatch)
    assert advice.source == "model" and len(advice.moves) == 3


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


# ---- titles, zero figures, reason codes, budget ------------------------------------------------------------------------------------

def test_titles_are_fixed_per_move_code_and_the_model_is_not_asked_for_one(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    model = Model(examples)
    advice = run(inp, facts, model, monkeypatch)
    assert [m.title_text for m in advice.moves] == [SPECS[m.move_code].title for m in advice.moves]
    sent = model.calls[0]["messages"][0]["content"] + model.calls[0]["messages"][1]["content"]
    assert "title" not in sent.lower()
    assert "title_text" not in AdvisorMove.model_fields


def test_a_zero_figure_has_its_own_wording_chosen_in_code(inputs, monkeypatch):
    inp, facts = inputs["C067"]                                            # saves $0 a month for retirement
    assert inp.candidates["increase_retirement"].facts["current_monthly"] == 0
    view = advisor.model_view(inp, facts)
    assert "aren't saving for retirement yet" in view and "You save about {retirement_monthly_now}" not in view
    tmpl = advisor.template_advice(inp)
    text = next(m.why_text for m in tmpl.moves if m.move_code == "increase_retirement")
    assert "aren't saving for retirement yet" in text and "$0" not in text
    # a model that ignores the variant and states the zero is rejected for it, and that move falls back to the template
    zero = ans(example_of(inp, "pay_card_high_apr"),
               ("increase_retirement", "You save about {retirement_monthly_now} a month toward {retirement_monthly_target}."))
    model = Model(zero)
    advice = run(inp, facts, model, monkeypatch)
    assert advice.diagnostics.first_reasons == ["zero_figure"] and not advice.diagnostics.first_try_ok
    assert [m for m in advice.moves if m.move_code == "increase_retirement"][0].source == "template"
    assert_grounded(advice, inp)


def test_other_zero_wordings():
    f = {"liquid": 0.0, "target": 1000.0, "gap": 1000.0, "months": 3, "current_monthly": 0.0, "automated_monthly": 0.0, "surplus": 100.0}
    for code in ("build_starter_cushion", "build_full_cushion", "automate_savings", "increase_retirement"):
        facts = {**f, "target_monthly": 500.0, "gap_monthly": 500.0}
        title, why = advisor.template_move(code, facts)
        assert "$0" not in why and "no cash" in why or "none of it" in why or "aren't saving" in why, code


def test_reason_codes_never_contain_model_text(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    own = example_of(inp, "build_full_cushion")
    cases = {
        "unknown_move_code": ans(own, ("not_a_move", "x")),
        "figure_not_in_facts": ans("You have {cushion_now}, which is $99,999 off."),
        "judgment": ans("Add {cushion_gap}; you are in good shape and safe."),
        "style": ans("Add {cushion_gap} using cushion_status."),
        "no_placeholder": ans("Build up your cash."),
        "missing_required_move": ans(None, ("automate_savings", example_of(inp, "automate_savings"))),
    }
    for reason, answer in cases.items():
        advice = run(inp, facts, Model(answer), monkeypatch)
        d = advice.diagnostics
        assert reason in d.first_reasons, (reason, d.first_reasons)
        assert d.retried and not d.first_try_ok and d.model_calls == 2
        assert all(re.fullmatch(r"[a-z_0-9]+", r) for r in d.first_reasons + d.final_reasons)        # codes, never prose
    flat = advisor.Inputs(inp.waterfall, inp.moves, inp.verdict,
                          {c: m.model_copy(update={'addresses_current_step': False}) for c, m in inp.candidates.items()}, inp.labels)
    assert "no_moves" in run(flat, facts, Model(ans(None)), monkeypatch).diagnostics.first_reasons
    ok = run(inp, facts, Model(examples), monkeypatch).diagnostics
    assert ok.first_try_ok and not ok.retried and ok.first_reasons == [] and ok.template_moves == 0 and ok.model_calls == 1


def test_model_failures_are_reason_codes(inputs, monkeypatch):
    inp, facts = inputs["C002"]
    assert run(inp, facts, Model(LLMUnavailable("down")), monkeypatch).diagnostics.first_reasons == ["llm_unavailable"]
    assert run(inp, facts, Model(LLMResponseError("junk")), monkeypatch).diagnostics.first_reasons == ["llm_bad_reply"]


class Slow:
    async def __call__(self, agent, messages, **kw):
        await asyncio.sleep(5)


def test_past_the_budget_the_template_is_served_and_nothing_fails(inputs, monkeypatch):
    inp, facts = inputs["C067"]
    monkeypatch.setattr(client, "complete", Slow())
    advice = asyncio.run(advisor.advise(inp, facts, budget_s=0.05))
    assert advice.source == "template" and all(m.source == "template" for m in advice.moves) and advice.moves
    assert advice.diagnostics.first_reasons == ["budget_exceeded"]
    assert_grounded(advice, inp)


def test_the_budget_comes_from_config(monkeypatch):
    from app.llm import config
    assert config.advisor_budget_s() == 12.0
    monkeypatch.setenv("ADVISOR_BUDGET_S", "3.5")
    assert config.advisor_budget_s() == 3.5


def test_dashboard_open_serves_the_template_when_the_advisor_is_over_budget(profiles, monkeypatch):
    from test_orchestrator import FakeLLM
    cache.clear()
    fake = FakeLLM()

    async def slow_advice(messages):
        await asyncio.sleep(5)
    fake.advice = slow_advice
    orig = fake.__call__

    async def call(agent, messages, **kw):
        if agent == "advisor":
            await asyncio.sleep(5)
        return await orig(agent, messages, **kw)
    monkeypatch.setattr(client, "complete", call)
    monkeypatch.setenv("ADVISOR_BUDGET_S", "0.2")
    r = asyncio.run(orchestrator.run("dashboard_open", profiles["C002"], {}))
    assert r.advice.source == "template" and r.degraded and r.summary.source in ("model", "template") and orchestrator.RUN_TIMEOUT_S == 30.0
    assert r.advice.diagnostics.first_reasons == ["budget_exceeded"]
    cache.clear()


def test_featured_personas_are_warmed_at_startup_in_the_background(profiles, monkeypatch):
    calls = []

    async def fake_run(event, profile, params, refresh=False):
        calls.append(profile.id)
        if profile.id == "C067":
            raise RuntimeError("boom")                                   # one failure must not stop the others
    monkeypatch.setattr(orchestrator, "run", fake_run)
    ids = ["C002", "C067", "C095", "NOPE"]
    monkeypatch.setenv("AGENT_WARM_ON_STARTUP", "false")
    asyncio.run(orchestrator.warm_featured(profiles, ids))
    assert calls == []
    monkeypatch.setenv("AGENT_WARM_ON_STARTUP", "true")
    asyncio.run(orchestrator.warm_featured(profiles, ids))
    assert calls == ["C002", "C067", "C095"]


def test_the_app_starts_the_warm_up_without_blocking_startup(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient
    started = []

    async def fake_warm(profiles, ids):
        started.append(list(ids))
        await asyncio.sleep(60)                                          # must not hold up the first request
    monkeypatch.setattr(orchestrator, "warm_featured", fake_warm)
    with TestClient(main.app) as c:
        assert c.get("/api/health").status_code in (200, 401, 404)
    assert started == [main.config.FEATURED_PERSONAS]


# ---- caching of temporary results ---------------------------------------------------------------------------------------------------

def test_the_cache_expires_entries_with_a_ttl(monkeypatch):
    import time
    cache.clear()
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    cache.put(("a",), 1, ttl=60)
    cache.put(("b",), 2)
    now[0] += 59
    assert cache.get(("a",)) == 1
    now[0] += 2
    assert cache.get(("a",)) is None and cache.get(("b",)) == 2
    cache.put(("a",), 3, ttl=60)
    cache.put(("a",), 4)                                                  # put again without a ttl: it no longer expires
    now[0] += 1000
    assert cache.get(("a",)) == 4
    cache.clear()


def _transient_setup(monkeypatch, advisor_error):
    from test_orchestrator import FakeLLM
    cache.clear()
    fake = FakeLLM()
    fake.advice = advisor_error
    monkeypatch.setattr(client, "complete", fake)
    monkeypatch.setattr(orchestrator, "RERUN_DELAY_S", 0.05)
    return fake


@pytest.mark.parametrize("reason,error", [("llm_unavailable", LLMUnavailable("down"))])
def test_a_template_result_from_an_unavailable_model_gets_a_short_ttl_and_one_background_rerun(profiles, monkeypatch, reason, error):
    fake = _transient_setup(monkeypatch, error)
    stored = []
    orig_put = cache.put
    monkeypatch.setattr(cache, "put", lambda key, value, ttl=None: (stored.append(ttl), orig_put(key, value, ttl))[1])

    async def go():
        r = await orchestrator.run("dashboard_open", profiles["C002"], {})
        assert r.advice.source == "template" and reason in r.advice.diagnostics.first_reasons and orchestrator.is_transient(r)
        assert stored == [60.0]
        fake.advice = lambda messages: __import__("test_orchestrator").adopt_examples(messages)      # the model is back
        await asyncio.sleep(0.5)                                          # the background rerun runs on its own
        again = await orchestrator.run("dashboard_open", profiles["C002"], {})
        assert orchestrator.is_cache_hit(again) and again.advice.source == "model" and not orchestrator.is_transient(again)
        assert stored[-1] is None and fake.calls.count("advise") == 2         # one failed call, one rerun; no further reruns
    asyncio.run(go())
    cache.clear()


def test_a_template_result_from_a_failed_check_is_cached_as_before(profiles, monkeypatch):
    fake = _transient_setup(monkeypatch, lambda messages: ans("Your cash is below the full cushion."))   # uses no figures: rejected, not transient
    stored = []
    orig_put = cache.put
    monkeypatch.setattr(cache, "put", lambda key, value, ttl=None: (stored.append(ttl), orig_put(key, value, ttl))[1])

    async def go():
        r = await orchestrator.run("dashboard_open", profiles["C002"], {})
        assert r.advice.moves[0].source == "template" and not orchestrator.is_transient(r)
        await asyncio.sleep(0.3)
        assert stored == [None] and fake.calls.count("advise") == 2           # two calls = the retry inside one run; no rerun
    asyncio.run(go())
    cache.clear()


def test_a_budget_miss_is_transient_too(profiles, monkeypatch):
    fake = _transient_setup(monkeypatch, None)

    async def slow(agent, messages, **kw):
        if agent == "advisor":
            await asyncio.sleep(5)
        return await fake(agent, messages, **kw)
    monkeypatch.setattr(client, "complete", slow)
    monkeypatch.setenv("ADVISOR_BUDGET_S", "0.05")
    r = asyncio.run(orchestrator.run("dashboard_open", profiles["C002"], {}))
    assert r.advice.diagnostics.first_reasons == ["budget_exceeded"] and orchestrator.is_transient(r)
    cache.clear()
