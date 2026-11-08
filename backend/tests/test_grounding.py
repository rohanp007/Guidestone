"""Grounding check, on C002's fact sheet (start $3,717.81, +$551.85 a month, low $1,628.85 on 2026-11-12, cushion $2,194.53,
below it on 3% of days until 2026-12-14, no overdraft, $10,342 at month 12)."""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import BaseModel

from app.data.loader import load_profiles
from app.formulas import forecast
from app.llm import client, grounding
from app.llm.facts import forecast_facts
from test_llm import reply, run, server  # noqa: F401  (the mocked-server fixture)


class Summary(BaseModel):
    summary: str


@pytest.fixture(scope="module")
def facts() -> dict:
    return forecast_facts(forecast.forecast_liquid_summary(load_profiles()["C002"], horizon_months=12))


def ok(text, facts, **kw):
    return grounding.check(text, facts, **kw)


def test_facts_have_end_balance_and_12_month_balance(facts):
    assert facts["end_balance"]["date"] == "2027-10-31" and facts["end_balance"]["p10"] < facts["end_balance"]["p50"] < facts["end_balance"]["p90"]
    assert facts["balance_in_12_months"]["p50"] == facts["end_balance"]["p50"]
    assert facts["chance_of_overdraft"] == 0 and 0.02 < facts["share_of_days_below_cushion"] < 0.04
    short = forecast_facts(forecast.forecast_liquid_summary(load_profiles()["C002"], horizon_months=6))
    assert "balance_in_12_months" not in short and short["end_balance"]["date"] == "2027-04-30"


def test_invented_figures_rejected(facts):
    r = ok("The balance reaches a high of $4,269.66.", facts)
    assert not r.ok and r.unmatched[0].text == "$4,269.66"
    assert not ok("You will have $4,870 by end of 2027.", facts).ok
    without_end = {k: v for k, v in facts.items() if k not in ("end_balance", "balance_in_12_months")}
    assert not ok("You will have $4,870 by end of 2027.", without_end).ok
    assert not ok("You will have about $10,300 by Oct 2027.", without_end).ok       # real number, but no longer in the facts


def test_rounded_and_exact_figures_accepted(facts):
    assert ok("You gain about $552 a month.", facts).ok
    assert ok("It dips to around $1,630 in November.", facts).ok
    assert ok("The low is $1,628.85 and $1,629 to the dollar.", facts).ok
    assert not ok("The low is $1,630.", facts).ok                                      # no qualifier: the cent or the nearest dollar
    assert not ok("You gain about $600 a month.", facts).ok                            # 9% off
    assert ok("That is about $6.6k a year.", facts).ok                                 # monthly gain x 12
    assert ok("You end about $6,600 ahead of where you started.", facts).ok            # end minus start
    assert ok("Roughly $10k by Oct 2027.", facts).ok and ok("About $10.3k at the end.", facts).ok


def test_percentages_and_ratios(facts):
    assert ok("You are below the cushion about 3% of days.", facts).ok
    assert ok("That is 3.0 percent of days below your cushion.", facts).ok
    assert not ok("You are below the cushion 8% of days.", facts).ok


def test_dates(facts):
    assert ok("You are below it until mid-December.", facts).ok
    assert ok("The low comes around Nov 12.", facts).ok and ok("The low comes on November 12, 2026.", facts).ok
    assert ok("Until December 14, 2026.", facts).ok and ok("About $10k by Oct 2027.", facts).ok
    assert not ok("The low comes around Nov 20.", facts).ok
    assert not ok("It stays tight until March 2027.", facts).ok
    assert not ok("The low comes in November 2027.", facts).ok                         # right month, wrong year
    assert ok("It may be tight.", facts).ok                                            # the verb "may" is not a date


def test_counts_and_context_numbers(facts):
    assert ok("Over the next 12 months you gain a lot.", facts).ok                     # the horizon is a fact
    assert not ok("Over the next 18 months you gain a lot.", facts).ok
    assert ok("Over the next 18 months you gain a lot.", facts, context_numbers=(18,)).ok
    assert not ok("It lasts 24 days.", facts).ok


def test_figure_under_the_wrong_fact_is_rejected(facts):
    bad = ok("Your overdraft risk is 3%.", facts)
    assert not bad.ok and bad.unmatched[0].text == "3%"
    assert ok("Your overdraft risk is 0%.", facts).ok
    assert not ok("Your overdraft is $2,195.", facts).ok                                # the cushion, not an overdraft figure
    assert not ok("You gain about $3,718 a month.", facts).ok                           # the starting balance, not the gain
    assert not ok("The lowest point is about $2,195.", facts).ok                        # the cushion, not the low point
    assert ok("The lowest point is about $1,629, under your $2,195 cushion.", facts).ok


def test_event_descriptions_supply_figures(facts):
    assert facts["next_events"], "C002 has scheduled events"
    first = facts["next_events"][0]
    pct = grounding.PERCENT.search(first["what"])
    assert pct and ok(f"Next comes a {pct.group(0)} change.", facts).ok


def test_report_lists_every_figure(facts):
    r = ok("You gain about $552 a month, with a low of $1,629 around Nov 12, below the cushion 3% of days.", facts)
    assert [n.kind for n in r.numbers].count("money") == 2 and len(r.numbers) == 4 and r.ok
    assert all(n.matched_fact for n in r.numbers)


def test_template_is_grounded_for_every_customer():
    for cid, profile in list(load_profiles().items())[:25]:
        f = forecast_facts(forecast.forecast_liquid_summary(profile, horizon_months=12))
        text = grounding.template(f)
        r = grounding.check(text, f)
        assert r.ok, (cid, text, [n.text for n in r.unmatched])


# ---- the retry / template loop, against the mocked server -----------------------------------------------------------------------

def _json(text: str):
    return reply(json.dumps({"summary": text}))


def go(facts):
    msgs = [{"role": "user", "content": "go"}]
    return run(grounding.grounded_complete("orchestrator", msgs, facts, Summary, context_numbers=(12,)))


def test_clean_answer_is_kept(server, facts):
    server.handler = lambda b: _json("You gain about $552 a month.")
    out = go(facts)
    assert out.text == "You gain about $552 a month." and not out.retried and not out.used_template and len(server.requests) == 1
    assert client.TRACE[-1] == {"kind": "grounding", "agent": "orchestrator", "numbers_checked": 1, "wording_conflicts": 0,
                                "retried": False,
                                "used_template": False, "ok": True}


def test_one_retry_names_the_bad_figures(server, facts):
    texts = iter(["It reaches a high of $4,269.66.", "You gain about $552 a month."])
    server.handler = lambda b: _json(next(texts))
    out = go(facts)
    assert out.text == "You gain about $552 a month." and out.retried and not out.used_template and len(out.calls) == 2
    sent = server.requests[1]["messages"]
    assert any(m["role"] == "user" and "These figures are not in the facts: $4,269.66. Rewrite using only figures from the facts." in m["content"]
               for m in sent)
    assert client.TRACE[-1]["retried"] is True and client.TRACE[-1]["used_template"] is False


def test_retry_that_still_invents_uses_the_template(server, facts):
    server.handler = lambda b: _json("It reaches a high of $4,269.66.")
    trace = client.start_run()
    out = go(facts)
    assert out.used_template and out.retried and out.text == grounding.template(facts) and out.report.ok
    assert "4,269" not in out.text and len(server.requests) == 2
    entry = trace[-1]
    assert entry["used_template"] is True and entry["retried"] is True and entry["ok"] is True and entry["numbers_checked"] > 0
    assert "4,269" not in json.dumps(trace)                                             # counts and flags only


# ---- status labels and wording -------------------------------------------------------------------------------------------------

def with_(facts, **labels):
    out = copy.deepcopy(facts)
    out.update(labels)
    return out


def test_status_labels_for_c002(facts):
    assert (facts["cushion_status"], facts["trend"], facts["overdraft_status"]) == ("dips_temporarily", "rising", "none")
    assert facts["cushion_status_until"] == "2026-12-14"


def test_status_label_rules():
    from datetime import date
    from app.llm.facts import status_labels
    d = date(2026, 12, 1)
    assert status_labels(5000, 2000, 0.0, None, 300, 0)["cushion_status"] == "stays_above"
    assert "cushion_status_until" not in status_labels(5000, 2000, 0.0, None, 300, 0)
    assert status_labels(5000, 2000, 0.1, d, 300, 0)["cushion_status"] == "dips_temporarily"
    assert status_labels(1500, 2000, 0.4, d, 300, 0)["cushion_status"] == "below_now"
    assert status_labels(5000, 2000, 0, None, 300, 0)["trend"] == "rising"
    assert status_labels(5000, 2000, 0, None, -300, 0)["trend"] == "falling"
    assert status_labels(5000, 2000, 0, None, 30, 0)["trend"] == "flat" and status_labels(5000, 2000, 0, None, -30, 0)["trend"] == "flat"
    assert [status_labels(5000, 2000, 0, None, 0, p)["overdraft_status"] for p in (0, 0.004, 0.2, 0.7)] == ["none", "none", "possible", "likely"]


def test_praise_of_the_cushion_is_rejected_when_it_dips(facts):
    for text in ("You have a large cushion.", "A comfortable cushion protects you.", "Your cushion is healthy.",
                 "The balance is always above the cushion.", "It never dips below the cushion.", "You sit comfortably above the cushion."):
        r = ok(text, facts)
        assert not r.ok and r.conflicts and r.conflicts[0].field == "cushion_status" and r.conflicts[0].status == "dips_temporarily", text
    assert ok("Your balance dips below your cushion until mid-December.", facts).ok
    assert ok("That is not a comfortable cushion.", facts).ok                            # a negation says the right thing
    assert not ok("You have a strong cushion.", with_(facts, cushion_status="below_now")).ok
    assert ok("You have a large cushion.", with_(facts, cushion_status="stays_above")).ok


def test_trend_wording(facts):
    assert ok("Your balance is growing.", facts).ok and ok("Your balance keeps rising.", facts).ok
    assert not ok("Your balance is shrinking.", facts).ok and not ok("Your balance is declining.", facts).ok
    assert ok("It dips to around $1,630 in November, then recovers.", facts).ok         # a dip is not a decline in trend
    falling = with_(facts, trend="falling")
    for text in ("Your balance is growing.", "Your balance is improving.", "Balances rise over the year."):
        assert not ok(text, falling).ok, text
    assert ok("Your balance is shrinking.", falling).ok
    assert not ok("Your balance is growing.", with_(facts, trend="flat")).ok and ok("Your balance is flat.", with_(facts, trend="flat")).ok


def test_overdraft_wording(facts):
    assert not ok("There is a risk of overdraft.", facts).ok and not ok("Overdraft risk is real.", facts).ok
    assert ok("There is no risk of overdraft.", facts).ok and ok("The risk of overdraft is minimal.", facts).ok
    assert not ok("There is no risk of overdraft.", with_(facts, overdraft_status="possible")).ok
    assert ok("There is a risk of overdraft.", with_(facts, overdraft_status="possible")).ok
    assert not ok("An overdraft is unlikely.", with_(facts, overdraft_status="likely")).ok


def test_stays_above_wording(facts):
    f = with_(facts, cushion_status="stays_above")
    assert not ok("You may fall short.", f).ok and not ok("You dip below the cushion in March.", f).ok
    assert ok("It never dips below the cushion.", f).ok and ok("A short-term view.", f).ok


def test_wording_without_labels_is_unchecked():
    assert grounding.check("A large cushion, always above.", {"cushion": 100.0}).ok


def test_conflict_gets_one_retry_naming_it(server, facts):
    texts = iter(["You have a large cushion.", "Your balance dips below your cushion until mid-December."])
    server.handler = lambda b: _json(next(texts))
    out = go(facts)
    assert out.retried and not out.used_template and len(out.calls) == 2
    msg = server.requests[1]["messages"]
    assert any('"large cushion" (cushion_status is dips_temporarily)' in m["content"] for m in msg if m["role"] == "user")
    assert client.TRACE[-1]["wording_conflicts"] == 0 and client.TRACE[-1]["retried"] is True


def test_persistent_conflict_uses_the_template(server, facts):
    server.handler = lambda b: _json("You have a large cushion.")
    out = go(facts)
    assert out.used_template and out.report.ok and not out.report.conflicts and out.text == grounding.template(facts)
    assert "dips below your cushion" in out.text and client.TRACE[-1]["used_template"] is True


# ---- overall status and judgment-free explanations -----------------------------------------------------------------------------

def test_overall_status_is_decided_in_code(facts):
    from app.llm.facts import overall_status as o
    assert facts["overall_status"] == "watch"                                       # C002: dips below the cushion, but rising
    assert o("stays_above", "rising", "none") == "on_track"
    assert o("dips_temporarily", "rising", "none") == "watch" and o("stays_above", "flat", "none") == "watch"
    assert o("below_now", "rising", "none") == "at_risk" and o("stays_above", "falling", "none") == "at_risk"
    assert o("stays_above", "rising", "possible") == "at_risk" and o("stays_above", "rising", "likely") == "at_risk"
    assert o("dips_temporarily", "flat", "none") == "watch" and o("dips_temporarily", "falling", "none") == "at_risk"


def test_evaluative_wording_only_checked_when_asked(facts):
    for text in ("Balances rise, posing no significant risk.", "You are safe.", "There is nothing to worry about.", "A healthy balance.",
                 "You are on track.", "Low risk overall.", "No risk here.", "That is reassuring.", "You are in good shape."):
        r = grounding.check(text, facts, judgments=True)
        assert not r.ok and any(c.field == "judgment" for c in r.conflicts), text
    for text in ("Your balance dips below your cushion until mid-December.", "The safety cushion is $2,195.",
                 "No simulated path overdraws.", "Rent renewal adds cost."):
        assert grounding.check(text, facts, judgments=True).ok, text
    assert grounding.check("Posing no significant risk.", facts).ok                  # off by default: only explanations are judgment-free


def test_template_is_judgment_free_for_every_customer():
    for cid, profile in list(load_profiles().items()):
        f = forecast_facts(forecast.forecast_liquid_summary(profile, horizon_months=12))
        r = grounding.check(grounding.template(f), f, judgments=True)
        assert r.ok, (cid, [c.phrase for c in r.conflicts])


def _explanation(text, hint=None):
    return reply(json.dumps({"explanation": text, "next_step_hint": hint}))


def explain_run(facts):
    return run(grounding.explain("orchestrator", [{"role": "user", "content": "go"}], facts, context_numbers=(12,)))


def test_explanation_with_a_judgment_is_retried_naming_it(server, facts):
    answers = iter([_explanation("Balances rise, with only a brief dip to $1,628.85 in November, posing no significant risk."),
                    _explanation("Balances rise about $552 a month, with a dip to $1,629 around Nov 12 under the cushion.", "Build the cushion")])
    server.handler = lambda b: next(answers)
    out = explain_run(facts)
    assert out.retried and not out.used_template and out.parsed.next_step_hint == "Build the cushion"
    sent = [m["content"] for m in server.requests[1]["messages"] if m["role"] == "user"]
    assert any("Do not state a judgment" in m and '"no significant risk"' in m for m in sent)


def test_persistent_judgment_falls_back_to_the_template(server, facts):
    server.handler = lambda b: _explanation("Nothing to worry about, you are on track.")
    out = explain_run(facts)
    assert out.used_template and out.report.ok and out.text == grounding.template(facts) and out.parsed.next_step_hint is None
    assert client.TRACE[-1]["used_template"] is True


def test_next_step_hint_is_checked_too(server, facts):
    server.handler = lambda b: _explanation("Balances rise about $552 a month.", "Move $5,000 into savings")
    out = explain_run(facts)
    assert out.used_template                                                         # $5,000 is not in the facts
    server.handler = lambda b: _explanation("Balances rise about $552 a month.", None)
    assert not explain_run(facts).used_template


def test_prompt_forbids_judgments_and_schema_is_fixed():
    from app.llm.prompts import FACTS_PROMPT
    from app.llm.schemas import Explanation
    assert "Do not state an overall judgment, risk level or reassurance. The app shows that separately." in FACTS_PROMPT
    assert set(Explanation.model_json_schema()["properties"]) == {"explanation", "next_step_hint"}


def test_bare_numbers_are_checked_too(facts):
    assert ok("The balance reaches 10342.27 by 2027-10-31.", facts).ok
    assert ok("A 551.85 monthly gain and 1,628.85 at the low.", facts).ok
    r = ok("The balance reaches 4269.66 by 2027-10-31.", facts)
    assert not r.ok and r.unmatched[0].text == "4269.66"
    assert not ok("The balance reaches 10342.99.", facts).ok and not ok("A gain of 551.85 a year... 3,100.", facts).ok
    assert not ok("Your cushion is 551.85.", facts).ok                              # a real number under the wrong fact
    # years, small whole numbers and ISO dates are not figure claims
    r = ok("In 2027, over 1-2 sentences, on 2026-11-12, p10 and p50 values.", facts)
    assert r.ok and [n.kind for n in r.numbers] == ["date"]
    assert ok("The raise is 3.1% and the rent change is 2.8%.", facts).ok
