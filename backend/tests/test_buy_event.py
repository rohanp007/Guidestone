"""The simulate_buy event with the models mocked: Researcher -> buy formulas -> advisor answer. The formulas, pack and grounding are real."""
from __future__ import annotations

import asyncio
import re

import pytest

from app.agent import cache, orchestrator, researcher
from app.agent.buy_advisor import AnswerModel
from app.data.loader import load_profiles
from app.llm import client
from app.llm.client import LLMResult, LLMUnavailable
from helpers import session_for

PARAMS = {"segment": "compact_suv", "budget": 36_000, "priorities": ["price", "safety"]}


@pytest.fixture(scope="module")
def profiles():
    return load_profiles()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cache.clear()
    monkeypatch.setattr(orchestrator, "RERUN_DELAY_S", 60.0)
    yield
    cache.clear()


class Fake:
    """Researcher: picks every listed id with a placeholder sentence (or `picks`). Advisor: copies the example sentence (or `advice`)."""
    def __init__(self, picks=None, advice=None, down=False):
        self.picks, self.advice, self.down, self.calls = picks, advice, down, []

    async def __call__(self, agent, messages, tools=None, json_schema=None, **kw):
        self.calls.append((agent, messages))
        if self.down:
            raise LLMUnavailable("down")
        if agent == "researcher":
            ids = re.findall(r"^- (\w+) \|", messages[1]["content"], re.M)
            picks = self.picks(ids) if callable(self.picks) else [{"id": i, "why": "It starts at {price} and gets {mpg} combined."} for i in ids]
            return LLMResult("{}", parsed=researcher.PicksModel(picks=[researcher.PickModel(**p) for p in picks]))
        assert agent == "advisor" and json_schema is AnswerModel
        ex = re.search(r"Example \(adapt it, keeping the braces\): (.*)", messages[1]["content"]).group(1)
        return LLMResult("{}", parsed=AnswerModel(answer=self.advice or ex))


def go(profile, fake, params=None, monkeypatch=None, **kw):
    client.complete = fake
    return asyncio.run(orchestrator.run("simulate_buy", profile, params or PARAMS, **kw))


@pytest.fixture(autouse=True)
def _restore():
    real = client.complete
    yield
    client.complete = real


def test_C095_affordable_cash(profiles):
    f = Fake()
    r = go(profiles["C095"], f)
    b = r.buy
    assert r.status == "affordable" and b.verdict == "affordable" and b.plans.recommended == "cash" and not r.degraded
    assert [p.id for p in b.picks] == ["honda_crv", "mazda_cx5"] and b.chosen == "honda_crv" and b.research_source == "model"     # priced cars under $35k band
    assert b.picks[0].why_text == "It starts at $30,920 and gets 30 mpg combined."
    assert {s.figure for s in b.picks[0].sources} == {"msrp", "mpg_combined", "nhtsa_stars"}
    assert all(s.url.startswith("https://") and s.fetched_at == "2026-11-08" and s.source_title for p in b.picks for s in p.sources)
    assert b.answer.source == "model" and "$30,920 in cash for the Honda CR-V" in b.answer.text and "$5,076 in loan interest" in b.answer.text
    assert [c[0] for c in f.calls] == ["researcher", "advisor"]
    assert [s.kind for s in r.trace if s.kind in ("research", "tool", "advise")] == ["research", "tool", "advise"]


def test_C002_is_not_now_and_the_answer_says_why(profiles):
    r = go(profiles["C002"], Fake(), {"segment": "midsize_sedan", "budget": 31_000, "priorities": ["mpg"]})
    b = r.buy
    assert b.verdict == "not_now" and b.plans.recommended == "finance" and b.chosen in ("honda_accord",)
    assert b.answer.source == "model" and "of your gross income" in b.answer.text and "under your cushion target" in b.answer.text
    assert "10.0%" in b.answer.text and "14.8%" in b.answer.text


def test_the_researcher_sees_only_the_sanitized_query_and_the_cars(profiles):
    f = Fake()
    go(profiles["C095"], f, {**PARAMS, "choice_id": "mazda_cx5"})
    messages = [m["content"] for a, ms in f.calls if a == "researcher" for m in ms]
    text = "\n".join(messages).lower()
    p = profiles["C095"]
    for secret in (p.id.lower(), str(p.credit_score), "6.15", "13.52", "apr", "credit", "gross", "mortgage", "67,902", "67902", "36,000", "choice_id"):
        assert secret not in text, secret
    assert "up to $35,000" in text and "honda_crv" in text and "price, safety" in text


def test_a_made_up_figure_gets_a_retry_then_the_template(profiles):
    bad = lambda ids: [{"id": ids[0], "why": "It is a great buy at $25,000 with 40 mpg."}]
    f = Fake(picks=bad)
    r = go(profiles["C095"], f)
    assert r.buy.research_source == "template" and r.degraded and [c[0] for c in f.calls].count("researcher") == 2
    assert all(p.source == "template" and "$30,920" in p.why_text or "$29,990" in p.why_text for p in r.buy.picks)
    assert not any("25,000" in p.why_text for p in r.buy.picks)
    cache.clear()
    unknown = lambda ids: [{"id": "tesla_model_3", "why": "{price}"}]
    assert go(profiles["C095"], Fake(picks=unknown)).buy.research_source == "template"


def test_model_off_serves_templates_everywhere(profiles):
    r = go(profiles["C095"], Fake(down=True))
    b = r.buy
    assert r.degraded and b.research_source == "template" and b.answer.source == "template" and b.verdict == "affordable"
    assert b.chosen == "mazda_cx5" and b.answer.text == "Paying $29,990 in cash for the Mazda CX-5 skips about $4,923 in loan interest."      # price first: the cheaper car
    assert len(b.picks) == 2 and b.picks[0].why_text.startswith("Mazda CX-5 starts at $29,990")


def test_the_top_three_by_code_score_follow_the_priorities():
    from app.research import pack
    from app.research.sanitize import sanitize
    entries = pack.eligible("midsize_sedan", None)
    assert [e["id"] for e in researcher.top_by_score(entries, sanitize({"segment": "midsize_sedan", "priorities": ["price"]}))] == ["honda_accord"]
    entries = pack.eligible("compact_suv", None)
    assert researcher.top_by_score(entries, sanitize({"segment": "compact_suv", "priorities": ["price"]}))[0]["id"] == "mazda_cx5"
    assert researcher.top_by_score(entries, sanitize({"segment": "compact_suv", "priorities": ["mpg"]}))[0]["id"] == "honda_crv"
    assert researcher.top_by_score(entries, sanitize({"segment": "compact_suv", "priorities": ["safety"]}))[0]["id"] in ("honda_crv", "mazda_cx5")      # one rating each, tie: price


def test_no_car_fits_returns_no_options_not_an_error(profiles):
    f = Fake()
    r = go(profiles["C095"], f, {"segment": "midsize_sedan", "budget": 15_000})
    b = r.buy
    assert r.status == "no_options" and b.no_options and b.picks == [] and b.chosen is None and b.plans is None and b.verdict is None
    assert b.answer.source == "template" and "nothing to compare" in b.answer.text and f.calls == []         # no model was asked
    assert r.facts == {} and not r.degraded


def test_nothing_priced_or_no_segment_results_is_also_no_options(profiles, monkeypatch):
    from app.research import pack
    monkeypatch.setattr(pack, "price", lambda v: None)                       # every price null
    assert go(profiles["C095"], Fake()).buy.no_options
    cache.clear()
    monkeypatch.undo()
    monkeypatch.setattr(pack, "vehicles", lambda segment=None: [])           # an empty segment
    assert go(profiles["C095"], Fake()).buy.no_options


def test_a_choice_not_among_the_picks_is_a_422_not_a_500(profiles):
    client.complete = Fake()
    c = session_for("C095")
    for body in ({**PARAMS, "choice_id": "honda_accord"},                                     # another segment
                 {**PARAMS, "budget": 30_000, "choice_id": "honda_crv"},                    # over the budget band
                 {"segment": "midsize_sedan", "budget": 15_000, "choice_id": "honda_accord"}):    # no options at all
        r = c.post("/api/me/agent/simulate_buy", json=body)
        assert r.status_code == 422, (body, r.status_code, r.text)
    ok = c.post("/api/me/agent/simulate_buy", json={"segment": "midsize_sedan", "budget": 15_000})
    assert ok.status_code == 200 and ok.json()["buy"]["no_options"] is True and ok.json()["buy"]["chosen"] is None and ok.json()["status"] == "no_options"


@pytest.mark.parametrize("body", [{}, {"segment": "compact_suv"}, {"budget": 0}, {"budget": 4_999}, {"budget": 10_000_000}, {"segment": "midsize_sedan", "budget": 29_000},
                                  {"priorities": []}, {"priorities": ["mpg", "mpg"]}, {"priorities": ["price", "mpg", "safety"]}, {"budget": 1.5},
                                  {"segment": "midsize_sedan", "priorities": ["safety"], "budget": 100_000}])
def test_no_valid_input_is_a_server_error(body):
    client.complete = Fake()
    for cid in ("C095", "C002", "C010"):
        r = session_for(cid).post("/api/me/agent/simulate_buy", json=body)
        assert r.status_code == 200, (cid, body, r.status_code, r.text[:200])
        assert r.json()["buy"] is not None
    for bad in ({"budget": -1}, {"segment": "boat"}, {"priorities": ["color"]}, {"priorities": ["price"] * 4}, {"budget": "lots"}, {"choice_id": 7}, {"choice_id": "nope"}):
        assert session_for("C095").post("/api/me/agent/simulate_buy", json=bad).status_code == 422, bad


def test_every_pick_carries_its_cited_figures(profiles):
    import json
    from pathlib import Path
    pack_ = {v["id"]: v for v in json.loads((Path(__file__).resolve().parent.parent / "app" / "research" / "cars.json").read_text())["vehicles"]}
    b = go(profiles["C095"], Fake()).buy
    assert [p.id for p in b.picks] == ["honda_crv", "mazda_cx5"]
    for p in b.picks:
        v = pack_[p.id]
        for name in ("msrp", "mpg_combined", "nhtsa_stars", "iihs_award"):
            f, rec = getattr(p.figures, name), v[name]
            assert f.value == rec["value"] and f.unit == rec["unit"] and f.model_year == rec["model_year"]
            if rec["url"]:
                assert (f.source.source_title, f.source.url, f.source.fetched_at) == (rec["source_title"], rec["url"], rec["fetched_at"])
            else:
                assert f.source is None
        assert p.figures.model_year.value == v["model_year"] and p.figures.model_year.source.url == v["msrp"]["url"]
    mazda = next(p for p in b.picks if p.id == "mazda_cx5").figures
    assert mazda.nhtsa_stars.value is None and mazda.nhtsa_stars.source.url.startswith("https://api.nhtsa.gov/")      # null, with where it looked
    assert mazda.msrp.value == 29_990 and mazda.iihs_award.value == "Top Safety Pick+"


def test_the_plan_carries_its_apr_source_and_the_recommended_plans_bands(profiles):
    p = go(profiles["C095"], Fake(), {**PARAMS, "choice_id": "honda_crv"}).buy.plans
    assert p.apr_source.url.startswith("https://www.bankrate.com/") and p.apr_source.fetched_at == "2026-11-08" and "Bankrate" in p.apr_source.source_title
    assert p.plans["finance"].apr_source == p.apr_source and p.plans["cash"].apr_source is None            # a cash plan has no loan
    assert len(p.baseline_monthly) == len(p.scenario_monthly) >= 6
    for m in p.scenario_monthly + p.baseline_monthly:
        assert m.p10 <= m.p50 <= m.p90
    assert p.scenario_monthly[0].p50 < p.baseline_monthly[0].p50                                          # cash leaves at month 0


def test_the_endpoint_and_the_stream(profiles):
    client.complete = Fake()
    c = session_for("C095")
    r = c.post("/api/me/agent/simulate_buy", json=PARAMS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event"] == "simulate_buy" and body["status"] == "affordable" and body["buy"]["plans"]["recommended"] == "cash" and body["payoff"] is None
    assert c.post("/api/me/agent/simulate_buy", json=PARAMS).headers["X-Cache"] == "hit"
    assert c.post("/api/me/agent/simulate_buy", json={**PARAMS, "choice_id": "toyota_rav4"}).status_code == 422          # no cited price
    assert c.post("/api/me/agent/simulate_buy", json={**PARAMS, "customer_id": "C002"}).status_code == 422
    assert c.post("/api/me/agent/simulate_buy", json={**PARAMS, "segment": "truck"}).status_code == 422
    cache.clear()
    import json
    events = [json.loads(b.split("data: ", 1)[1]) for b in c.post("/api/me/agent/simulate_buy/stream", json=PARAMS).text.strip().split("\n\n")]
    labels = [(e.get("agent"), e.get("phase"), e.get("label")) for e in events]
    order = [labels.index(x) for x in [("researcher", "start", "Choosing cars to compare"), ("researcher", "done", "Cars chosen"),
                                       ("compute", "start", "Comparing 3 ways to pay"), ("compute", "step", "Ways to pay compared"),
                                       ("advisor", "start", "Writing the answer"), ("advisor", "done", "Answer ready")]]
    assert order == sorted(order) and events[-1]["type"] == "result" and events[-1]["result"]["buy"]["verdict"] == "affordable"
    assert not any(k in e for e in events[:-1] for k in ("result", "prompt", "content"))


def test_C067_answer_mentions_the_high_apr_debt(profiles):
    params = {"segment": "midsize_sedan", "budget": 30_000, "priorities": ["price"], "choice_id": "honda_accord"}
    r = go(profiles["C067"], Fake(), params)
    b = r.buy
    assert b.verdict == "stretch" and b.plans.recommended == "mix" and b.plans.plans["cash"].status == "infeasible"
    assert b.answer.source == "model" and "24.8% a year" in b.answer.text and len(re.findall(r"(?<=[a-z%])\.\s", b.answer.text + " ")) == 2
    cache.clear()
    quiet = "Putting {down_payment} down on the {car} means a {payment} monthly payment."            # leaves the debt out
    f = Fake(advice=quiet)
    r = go(profiles["C067"], f, params)
    assert r.buy.answer.source == "template" and "24.8% a year" in r.buy.answer.text and [c[0] for c in f.calls].count("advisor") == 2
    assert "high_apr_debt" in str(r.buy.answer.diagnostics.first_reasons) or "warning_not_mentioned" in r.buy.answer.diagnostics.first_reasons


def test_C002_and_C095_answers_are_unchanged_in_kind(profiles):
    assert go(profiles["C095"], Fake()).buy.answer.text.endswith("in loan interest.")
    cache.clear()
    r = go(profiles["C002"], Fake(), {"segment": "midsize_sedan", "budget": 31_000, "priorities": ["mpg"]})
    assert r.buy.verdict == "not_now" and "high_apr_debt" not in r.buy.answer.text
