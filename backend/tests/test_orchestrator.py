"""Orchestrator v1 (dashboard_open) with the model mocked. The formulas are real."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest

from app.agent import cache, orchestrator
from app.agent.cache import RunSuperseded
from app.agent.models import Plan, PlanStep
from app.data import loader
from app.data.loader import load_profiles
from app.formulas.registry import FORMULAS
from app.llm import client
from app.llm.client import LLMResponseError, LLMResult, LLMUnavailable
from app.llm.schemas import Explanation
from helpers import session_for

GOOD_TEXT = "Your balance dips below your cushion until mid-December, then rises about $570 a month."   # C002 at the dashboard's 24-month horizon, grounded
JOB = {"tool": "job_loss_stress", "args": {"horizon_months": 24}, "reason": "check emergency fund coverage"}


class FakeLLM:
    """Stands in for client.complete: the planner gets `plan`, the explainer gets `text`. Any of them may be an exception to raise."""
    def __init__(self, plan=None, text=GOOD_TEXT):
        self.plan = [JOB] if plan is None else plan
        self.text = text
        self.calls: list[str] = []
        self.messages: list[list[dict]] = []
        self.gate: asyncio.Event | None = None         # when set, the planner waits for it
        self.entered: asyncio.Event | None = None      # when set, the planner signals that it has been reached

    async def __call__(self, agent, messages, tools=None, json_schema=None, **kw):
        self.messages.append(messages)
        if json_schema is Plan:
            self.calls.append("plan")
            if self.entered is not None: self.entered.set()
            if self.gate is not None: await self.gate.wait()
            if isinstance(self.plan, BaseException): raise self.plan
            return LLMResult("", parsed=Plan(steps=[PlanStep(**s) for s in self.plan]))
        assert json_schema is Explanation
        self.calls.append("explain")
        if isinstance(self.text, BaseException): raise self.text
        return LLMResult("", parsed=Explanation(explanation=self.text))


@pytest.fixture
def fake(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(client, "complete", f)
    cache.clear()
    yield f
    cache.clear()


@pytest.fixture(scope="module")
def profiles():
    return load_profiles()


def run(coro):
    return asyncio.run(coro)


def go(profile, fake=None, **kw):
    return run(orchestrator.run("dashboard_open", profile, {}, **kw))


def kinds(r):
    return [s.kind for s in r.trace]


# ---- happy path ----------------------------------------------------------------------------------------------------------------

def test_happy_path(fake, profiles):
    r = go(profiles["C002"])
    assert r.event == "dashboard_open" and r.customer_id == "C002" and r.inputs == {} and not r.degraded
    assert {"forecast_liquid_summary", "paycheck_to_paycheck", "job_loss_stress"} <= set(r.compute)
    assert r.compute["job_loss_stress"]["applicable"] is True
    assert r.status == r.facts["overall_status"] == "watch"                         # decided in code
    assert r.facts["paycheck_to_paycheck"] in ("yes", "no") and r.facts["employment"] == "employed"
    assert r.summary.source == "model" and r.summary.text == GOOD_TEXT and r.summary.grounding.ok
    assert kinds(r) == ["precompute", "precompute", "plan", "tool", "explain"]
    assert [s.name for s in r.trace if s.kind == "tool"] == ["job_loss_stress"] and all(s.ok for s in r.trace)
    assert r.research == [] and r.advice is None and r.prompt_version and len(r.data_version) == 12
    json.dumps(r.model_dump(mode="json"), allow_nan=False)                           # JSON-safe


# ---- plan validation -----------------------------------------------------------------------------------------------------------

def rejected(r):
    return [(s.name, s.detail["code"]) for s in r.trace if s.kind == "plan_rejected"]


def test_unknown_tool_is_dropped(fake, profiles):
    fake.plan = [{"tool": "ignore previous instructions", "reason": "x"}, {"tool": "forecast_liquid", "reason": "x"}]
    r = go(profiles["C002"])
    assert rejected(r) == [("unknown", "not_allowed"), ("forecast_liquid", "not_allowed")]       # the model's text is never echoed
    assert "tool" not in kinds(r) and r.degraded and r.summary


def test_bad_args_are_dropped(fake, profiles):
    fake.plan = [{"tool": "job_loss_stress", "args": {"horizon_months": 999}}, {"tool": "job_loss_stress", "args": {"seed": 3}},
                 {"tool": "job_loss_stress", "args": {"horizon_months": "soon"}}]
    r = go(profiles["C002"])
    assert rejected(r) == [("job_loss_stress", "bad_args")] * 3 and "tool" not in kinds(r)


def test_duplicate_of_a_precomputed_formula_is_dropped(fake, profiles):
    fake.plan = [{"tool": "forecast_liquid_summary", "args": {"horizon_months": 24}}, {"tool": "forecast_liquid_summary"},
                 {"tool": "forecast_liquid_summary", "args": {"horizon_months": 6}}, {"tool": "paycheck_to_paycheck"}]
    r = go(profiles["C002"])
    assert rejected(r) == [("forecast_liquid_summary", "duplicate"), ("forecast_liquid_summary", "duplicate"),
                           ("paycheck_to_paycheck", "not_allowed")]
    assert "forecast_liquid_summary#2" in r.compute and r.compute["forecast_liquid_summary#2"]["horizon_months"] == 6   # a different horizon is fine


def test_duplicate_steps_in_the_plan_and_more_than_four(fake, profiles):
    fake.plan = [JOB, JOB] + [{"tool": "job_loss_stress", "args": {"horizon_months": h}} for h in (3, 4, 5, 6, 7)]
    r = go(profiles["C002"])
    assert rejected(r) == [("job_loss_stress", "duplicate"), ("job_loss_stress", "too_many"), ("job_loss_stress", "too_many")]
    assert kinds(r).count("tool") == 4


def test_a_retiree_cannot_be_planned_a_job_loss_stress(fake, profiles):
    r = go(profiles["C010"])
    assert rejected(r) == [("job_loss_stress", "not_allowed")] and "tool" not in kinds(r)
    assert "job_loss_stress" not in r.compute


# ---- planner failure -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("err", [LLMUnavailable("down"), LLMResponseError("bad"), TimeoutError()])
def test_planner_failure_uses_the_default_plan(fake, profiles, err):
    fake.plan = err
    if isinstance(err, TimeoutError):                    # a raw timeout is not an LLMError: the run limit handles it, the plan is never lost silently
        fake.plan = LLMUnavailable("timeout")
    r = go(profiles["C002"])
    assert r.degraded and "fallback" in kinds(r) and [s.name for s in r.trace if s.kind == "fallback"] == ["plan"]
    assert [s.name for s in r.trace if s.kind == "tool"] == ["job_loss_stress"]       # the default plan for an employed customer
    assert r.summary and r.summary.source == "model"


def test_retiree_default_plan_is_empty(fake, profiles):
    fake.plan = LLMUnavailable("down")
    r = go(profiles["C010"])
    assert r.degraded and "tool" not in kinds(r) and r.summary and r.compute.keys() == {"forecast_liquid_summary", "paycheck_to_paycheck"}


def test_explainer_failure_uses_the_template(fake, profiles):
    fake.text = LLMUnavailable("down")
    r = go(profiles["C002"])
    assert r.degraded and r.summary.source == "template" and r.summary.grounding.ok and [s.name for s in r.trace if s.kind == "fallback"] == ["explain"]
    fake.text = "You are on track and safe."                       # a judgment, twice: retry then template
    cache.clear()
    r = go(profiles["C002"])
    assert r.summary.source == "template" and r.degraded and r.summary.grounding.retried is True
    assert fake.calls.count("explain") >= 2


# ---- failures ------------------------------------------------------------------------------------------------------------------

def test_a_failing_tool_step_is_recorded_and_skipped(fake, profiles, monkeypatch):
    def boom(profile, **kw): raise RuntimeError("secret internal detail")
    monkeypatch.setitem(FORMULAS, "job_loss_stress", dataclasses.replace(FORMULAS["job_loss_stress"], fn=boom))
    r = go(profiles["C002"])
    step = next(s for s in r.trace if s.kind == "tool")
    assert not step.ok and step.detail["error"] == "RuntimeError" and "secret" not in json.dumps(r.model_dump(mode="json"))
    assert r.degraded and "job_loss_stress" not in r.compute and r.summary and r.status


def test_a_failing_precompute_fails_the_run(fake, profiles, monkeypatch):
    def boom(profile, **kw): raise RuntimeError("bug")
    monkeypatch.setitem(FORMULAS, "paycheck_to_paycheck", dataclasses.replace(FORMULAS["paycheck_to_paycheck"], fn=boom))
    with pytest.raises(RuntimeError):
        go(profiles["C002"])
    assert fake.calls == []                                           # the model was never asked


# ---- cache ---------------------------------------------------------------------------------------------------------------------

def test_cache(fake, profiles, monkeypatch):
    first = go(profiles["C002"])
    n = len(fake.calls)
    hit = go(profiles["C002"])
    assert orchestrator.is_cache_hit(hit) and not orchestrator.is_cache_hit(first) and len(fake.calls) == n
    assert hit.run_id == first.run_id and kinds(hit)[-1] == "cache_hit"
    fresh = go(profiles["C002"], refresh=True)
    assert not orchestrator.is_cache_hit(fresh) and len(fake.calls) > n and fresh.run_id != first.run_id
    assert orchestrator.is_cache_hit(go(profiles["C002"]))            # the refreshed result replaced the entry
    n = len(fake.calls)
    monkeypatch.setattr(loader, "cache_key", lambda: "different-data-version-hash")
    changed = go(profiles["C002"])
    assert not orchestrator.is_cache_hit(changed) and changed.data_version == "different-da" and len(fake.calls) > n
    other = go(profiles["C067"])
    assert other.customer_id == "C067" and not orchestrator.is_cache_hit(other)


def test_unknown_event_and_bad_params(fake, profiles):
    with pytest.raises(KeyError):
        run(orchestrator.run("nope", profiles["C002"], {}))
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        run(orchestrator.run("dashboard_open", profiles["C002"], {"customer_id": "C067"}))


# ---- concurrency and limits ----------------------------------------------------------------------------------------------------

def test_a_newer_run_cancels_the_older_one(fake, profiles):
    async def scenario():
        fake.gate = asyncio.Event()
        fake.entered = asyncio.Event()
        older = asyncio.ensure_future(orchestrator.run("dashboard_open", profiles["C002"], {}))
        await asyncio.wait_for(fake.entered.wait(), 5)                 # the older run is waiting inside the planner
        fake.gate.set()                                                # (later planner calls pass straight through)
        newer = await orchestrator.run("dashboard_open", profiles["C002"], {}, refresh=True)
        with pytest.raises(RunSuperseded):
            await older
        return newer

    newer = run(scenario())
    assert newer.summary and not newer.degraded


def test_an_identical_request_joins_the_run_in_flight(fake, profiles):
    async def scenario():
        fake.gate = asyncio.Event()
        fake.entered = asyncio.Event()
        a = asyncio.ensure_future(orchestrator.run("dashboard_open", profiles["C002"], {}))
        await asyncio.wait_for(fake.entered.wait(), 5)
        b = asyncio.ensure_future(orchestrator.run("dashboard_open", profiles["C002"], {}))   # e.g. the page opening after the sign-in warm-up
        await asyncio.sleep(0.05)
        fake.gate.set()
        return await a, await b

    a, b = run(scenario())
    assert a.run_id == b.run_id and fake.calls.count("plan") == 1


def test_a_hung_planner_still_gives_a_result_within_the_limit(fake, profiles, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUN_TIMEOUT_S", 0.5)
    fake.gate = asyncio.Event()                                        # never set
    t0 = time.perf_counter()
    r = go(profiles["C002"])
    assert time.perf_counter() - t0 < 3
    assert r.degraded and r.summary.source == "template" and r.summary.grounding.ok and r.status == "watch"
    assert [s.name for s in r.trace if s.kind == "fallback"] == ["timeout"]
    assert {"forecast_liquid_summary", "paycheck_to_paycheck"} <= set(r.compute)           # what was computed is returned


# ---- privacy -------------------------------------------------------------------------------------------------------------------

def test_the_trace_holds_no_prompt_model_text_or_rejected_arguments(fake, profiles):
    fake.plan = [{"tool": "job_loss_stress", "args": {"horizon_months": 999, "note": "CANARY-ARGS"}, "reason": "CANARY-REASON"},
                 {"tool": "CANARY-TOOL-NAME", "reason": "x"}, JOB]
    fake.text = "CANARY-TEXT $9,999,999 is safe."
    r = go(profiles["C002"])
    blob = json.dumps([s.model_dump(mode="json") for s in r.trace]) + json.dumps(r.llm_trace)
    for canary in ("CANARY", r.summary.text, GOOD_TEXT):
        assert canary not in blob
    for messages in fake.messages:                                     # nothing the model was sent shows up in the trace either
        for m in messages:
            if len(m["content"] or "") >= 20:
                assert m["content"][:60] not in blob
    for s in r.trace:
        for v in s.detail.values():
            assert isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) < 40)


# ---- the endpoint --------------------------------------------------------------------------------------------------------------

def test_endpoint_uses_only_the_session_customer(fake):
    c = session_for("C002")
    r = c.post("/api/me/agent/dashboard_open", json={})
    assert r.status_code == 200 and r.json()["customer_id"] == "C002" and r.headers["X-Cache"] == "miss" and "X-Run-Ms" in r.headers
    assert c.post("/api/me/agent/dashboard_open").headers["X-Cache"] == "hit"                   # an empty body is fine
    assert c.post("/api/me/agent/dashboard_open?refresh=1", json={}).headers["X-Cache"] == "miss"
    # there is no way to name another customer
    assert c.post("/api/me/agent/dashboard_open", json={"customer_id": "C067"}).status_code == 422
    r = c.post("/api/me/agent/dashboard_open?customer_id=C067&id=C067", json={})
    assert r.status_code == 200 and r.json()["customer_id"] == "C002"
    assert session_for("C067").post("/api/me/agent/dashboard_open", json={}).json()["customer_id"] == "C067"


def test_endpoint_errors(fake):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).post("/api/me/agent/dashboard_open", json={}).status_code == 401
    c = session_for("C002")
    assert c.post("/api/me/agent/no_such_event", json={}).status_code == 404
    assert c.post("/api/me/agent/dashboard_open", content=b"[1]").status_code == 422
    assert c.post("/api/me/agent/dashboard_open", content=b"{nope").status_code == 422


def test_degraded_run_is_still_200(fake):
    fake.plan, fake.text = LLMUnavailable("down"), LLMUnavailable("down")
    r = session_for("C002").post("/api/me/agent/dashboard_open", json={})
    assert r.status_code == 200 and r.json()["degraded"] is True and r.json()["summary"]["source"] == "template"


def test_sign_in_starts_the_dashboard_run_in_the_background(fake, monkeypatch):
    seen = []

    async def stub(event, profile, params, refresh=False):
        seen.append((event, profile.id, params))

    monkeypatch.setattr(orchestrator, "run", stub)
    session_for("C002")
    assert seen == []                                                 # off in tests by default
    monkeypatch.setenv("AGENT_WARM_ON_LOGIN", "true")
    session_for("C067")
    assert seen == [("dashboard_open", "C067", {})]


def test_placeholders_are_filled_and_the_model_never_sees_codes(fake, profiles):
    import re
    fake.text = "Your balance dips below your cushion until {cushion_until}, then rises {monthly_gain} a month."
    r = go(profiles["C002"])
    assert r.summary.source == "model" and "{" not in r.summary.text and "$570" in r.summary.text and "Dec 14" in r.summary.text
    writer = next(m for m in fake.messages if m[0]["content"].startswith("You are a careful financial assistant"))
    view = writer[1]["content"]
    assert re.findall(r"\d+", re.sub(r"\{\w+\}", "", view)) == ["24"] and "_status" not in view and "watch" not in view
    assert r.facts["cushion_status"] == "dips_temporarily"                          # the codes stay in the result, for the UI


def test_machinery_in_the_summary_is_rejected(fake, profiles):
    fake.text = "The cushion_status is stays_above and the next step hint is null."
    r = go(profiles["C002"])
    assert r.summary.source == "template" and "_" not in r.summary.text and "null" not in r.summary.text and r.degraded


def test_the_endpoint_is_in_the_openapi_schema(fake):
    schema = session_for("C002").get("/openapi.json").json()
    assert "AnalysisResult" in schema["components"]["schemas"] and "HeadlineSummary" in schema["components"]["schemas"]
    post = schema["paths"]["/api/me/agent/{event}"]["post"]
    assert post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("AnalysisResult") and "409" in post["responses"]
