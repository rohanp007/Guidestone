"""The /stream endpoint and the orchestrator's progress events: order, cached replay, joining a run in flight, errors, and what never appears."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent import cache, orchestrator, stream as sm
from app.agent.stream import LABEL_TEXTS, StreamDetail
from app.data.loader import load_profiles
from app.formulas.registry import FORMULAS
from app.llm import client
from helpers import session_for
from test_orchestrator import FakeLLM
from test_payoff_event import Model, example

TOP_KEYS = {"type", "run_id", "seq", "t_ms", "agent", "phase", "label", "detail", "result"}
DETAIL_KEYS = {"formula", "ms", "check", "outcome", "cached"}


@pytest.fixture(scope="module")
def profiles():
    return load_profiles()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cache.clear()
    monkeypatch.setattr(orchestrator, "RERUN_DELAY_S", 60.0)
    yield
    cache.clear()


async def collect(event, profile, params=None, **kw):
    it = await orchestrator.open_stream(event, profile, params or {}, **kw)
    return [ev async for ev in it]


def labels(events, phase=None):
    return [(e.agent, e.phase, e.label) for e in events if e.type == "progress" and (phase is None or e.phase == phase)]


def index(events, agent, phase, label):
    return next(i for i, e in enumerate(events) if (e.agent, e.phase, e.label) == (agent, phase, label))


def check_envelope(events):
    assert [e.seq for e in events] == list(range(len(events)))
    assert [e.t_ms for e in events] == sorted(e.t_ms for e in events)
    assert events[-1].type in ("result", "error") and all(e.type == "progress" for e in events[:-1])


# ---- order ----------------------------------------------------------------------------------------------------------------------

def test_simulate_payoff_event_order(profiles, monkeypatch):
    monkeypatch.setattr(client, "complete", Model(example))
    ev = asyncio.run(collect("simulate_payoff", profiles["C067"]))
    check_envelope(ev)
    assert (ev[0].agent, ev[0].phase, ev[0].label) == ("orchestrator", "start", "Starting the check-up")
    assert (ev[-2].agent, ev[-2].phase, ev[-2].label) == ("orchestrator", "done", "Check-up ready") and ev[-1].type == "result"
    order = [index(ev, "compute", "start", "Comparing 5 payoff strategies"), index(ev, "compute", "step", "Strategies compared"),
             index(ev, "compute", "start", "Simulating the chosen plan"), index(ev, "compute", "step", "Plan simulated"),
             index(ev, "advisor", "start", "Writing the answer"), index(ev, "advisor", "step", "Checking the answer against the numbers"),
             index(ev, "advisor", "done", "Answer ready"), len(ev) - 2]
    assert order == sorted(order) and len(set(order)) == len(order)
    check = ev[order[5]]
    assert check.detail.check == "grounding" and check.detail.outcome == "passed"
    assert ev[order[1]].detail.formula == "payoff_strategies_summary" and ev[order[1]].detail.ms is not None
    r = ev[-1].result
    assert r.event == "simulate_payoff" and r.run_id == ev[0].run_id and r.payoff.verdict == "pay_with_tradeoff" and r.payoff.answer.source == "model"


def test_dashboard_open_event_order(profiles, monkeypatch):
    monkeypatch.setattr(client, "complete", FakeLLM())
    ev = asyncio.run(collect("dashboard_open", profiles["C067"]))
    check_envelope(ev)
    assert (ev[0].agent, ev[0].phase) == ("orchestrator", "start") and (ev[-2].label, ev[-1].type) == ("Check-up ready", "result")
    assert index(ev, "compute", "start", "Running the calculations") < index(ev, "compute", "step", "Forecasting your cash") \
        < index(ev, "compute", "done", "Calculations ready")
    assert index(ev, "orchestrator", "start", "Planning which calculations to run") < index(ev, "orchestrator", "done", "Calculations chosen")
    assert index(ev, "compute", "step", "Testing what a job loss would do") > index(ev, "orchestrator", "done", "Calculations chosen")
    assert index(ev, "compute", "step", "Applying the decision rules")
    assert index(ev, "presenter", "start", "Writing the summary") < index(ev, "presenter", "done", "Summary written")
    assert index(ev, "advisor", "start", "Choosing your next moves") < index(ev, "advisor", "step", "Checking the answer against the numbers") \
        < index(ev, "advisor", "done", "Next moves ready")
    assert ev[-1].result.event == "dashboard_open" and ev[-1].result.advice.source == "model"


def test_a_failed_plan_and_a_template_answer_say_so(profiles, monkeypatch):
    from app.llm.client import LLMUnavailable
    f = FakeLLM(plan=LLMUnavailable("down"))
    f.advice = LLMUnavailable("down")
    monkeypatch.setattr(client, "complete", f)
    ev = asyncio.run(collect("dashboard_open", profiles["C067"]))
    assert ("orchestrator", "done", "Using the standard set of calculations") in labels(ev)
    assert any(e.label == "Using the standard wording" and e.detail.outcome == "template" for e in ev if e.type == "progress")


# ---- cached replay --------------------------------------------------------------------------------------------------------------

def test_a_cached_result_replays_its_trace_at_once(profiles, monkeypatch):
    m = Model(example)
    monkeypatch.setattr(client, "complete", m)
    first = asyncio.run(collect("simulate_payoff", profiles["C067"]))
    calls = len(m.calls)
    again = asyncio.run(collect("simulate_payoff", profiles["C067"]))
    assert len(m.calls) == calls                                              # nothing ran
    check_envelope(again)
    assert all(e.detail.cached is True for e in again if e.type == "progress") and all(e.t_ms == 0 for e in again)
    kept = lambda ev: [x for x in labels(ev) if x[1] != "start"]                # starts are not in the stored trace: the replay has the steps and finishes
    assert kept(again) == kept(first) and again[0].phase == "start"
    assert again[-1].type == "result" and again[-1].result.trace[-1].kind == "cache_hit" and again[-1].result.payoff.verdict == "pay_with_tradeoff"
    assert all(e.detail.cached is None for e in first if e.type == "progress")


# ---- joining a run in flight ----------------------------------------------------------------------------------------------------

def test_a_second_stream_joins_the_run_in_flight(profiles, monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(client, "complete", f)

    async def scenario():
        f.gate, f.entered = asyncio.Event(), asyncio.Event()
        a = asyncio.ensure_future(collect("dashboard_open", profiles["C067"]))
        await f.entered.wait()
        b = asyncio.ensure_future(collect("dashboard_open", profiles["C067"]))          # arrives mid-run: sees what already happened, then the rest
        await asyncio.sleep(0.05)
        f.gate.set()
        return await a, await b

    a, b = asyncio.run(scenario())
    assert f.calls.count("plan") == 1
    assert [e.run_id for e in a] == [e.run_id for e in b] and len({e.run_id for e in a}) == 1
    assert labels(a) == labels(b) and a[0].detail.cached is None and b[0].label == "Starting the check-up"
    assert b[-1].type == "result" and b[-1].result.run_id == a[-1].result.run_id


def test_a_stream_joins_a_run_started_without_one(profiles, monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(client, "complete", f)

    async def scenario():
        f.gate, f.entered = asyncio.Event(), asyncio.Event()
        warm = asyncio.ensure_future(orchestrator.run("dashboard_open", profiles["C067"], {}))      # the sign-in warm-up
        await f.entered.wait()
        joined = asyncio.ensure_future(collect("dashboard_open", profiles["C067"]))
        await asyncio.sleep(0.05)
        f.gate.set()
        return await warm, await joined

    result, ev = asyncio.run(scenario())
    assert f.calls.count("plan") == 1 and ev[0].label == "Starting the check-up" and ev[-1].result.run_id == result.run_id
    assert ev[-1].result.trace[-1].kind != "cache_hit"


def test_a_subscriber_that_leaves_does_not_stop_the_run(profiles, monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(client, "complete", f)

    async def scenario():
        f.gate, f.entered = asyncio.Event(), asyncio.Event()
        it = await orchestrator.open_stream("dashboard_open", profiles["C067"], {})
        first = await it.__anext__()
        await f.entered.wait()
        await it.aclose()
        f.gate.set()
        for _ in range(200):
            await asyncio.sleep(0.05)
            if cache.get(cache.cache_key("C067", "dashboard_open", {}, orchestrator.PROMPT_VERSION, orchestrator.loader.cache_key()[:12])):
                break
        return first

    assert asyncio.run(scenario()).label == "Starting the check-up"
    assert f.calls.count("plan") == 1


# ---- errors ---------------------------------------------------------------------------------------------------------------------

def test_a_failed_run_ends_with_an_error_event_and_no_details(profiles, monkeypatch):
    monkeypatch.setattr(client, "complete", FakeLLM())

    async def boom(st):
        raise RuntimeError("secret account 12345 balance $9,999")

    monkeypatch.setattr(orchestrator, "precompute", boom)
    ev = asyncio.run(collect("dashboard_open", profiles["C067"]))
    check_envelope(ev)
    assert (ev[-2].agent, ev[-2].phase, ev[-2].label) == ("orchestrator", "error", "The check-up could not be completed")
    assert ev[-1].type == "error" and ev[-1].result is None
    assert "12345" not in json.dumps([e.model_dump(mode="json") for e in ev])


def test_a_superseded_run_says_so(profiles, monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(client, "complete", f)

    async def scenario():
        f.gate, f.entered = asyncio.Event(), asyncio.Event()
        a = asyncio.ensure_future(collect("dashboard_open", profiles["C067"]))
        await f.entered.wait()
        f.gate.set()
        b = asyncio.ensure_future(collect("dashboard_open", profiles["C067"], refresh=True))
        return await a, await b

    a, b = asyncio.run(scenario())
    assert a[-1].type == "error" and a[-2].label == "A newer check-up replaced this one"
    assert b[-1].type == "result"


# ---- what never appears ---------------------------------------------------------------------------------------------------------

def test_no_event_carries_prompts_model_text_or_customer_data(profiles, monkeypatch):
    assert set(StreamDetail.model_fields) == DETAIL_KEYS
    m = Model(example)
    monkeypatch.setattr(client, "complete", m)
    events = asyncio.run(collect("simulate_payoff", profiles["C067"]))
    cache.clear()
    monkeypatch.setattr(client, "complete", FakeLLM())
    events += asyncio.run(collect("dashboard_open", profiles["C067"]))
    events += asyncio.run(collect("dashboard_open", profiles["C067"]))                       # the replay too
    assert len(events) > 25
    sentences = [e.result.payoff.answer.text for e in events if e.result and e.result.payoff] + \
                [e.result.summary.text for e in events if e.result and e.result.summary]              # what the models wrote
    assert len(sentences) == 3
    for e in events:
        wire = e.model_dump(mode="json", exclude={"result"}, exclude_none=True)
        assert set(wire) <= TOP_KEYS and set(wire.get("detail", {})) <= DETAIL_KEYS
        if e.type == "progress":
            assert e.label in LABEL_TEXTS and (e.detail.formula is None or e.detail.formula in FORMULAS or e.detail.formula == "decision_rules")
            assert e.detail.check in (None, "grounding") and e.detail.outcome in (None, "passed", "retried", "template")
        text = json.dumps(wire)
        for banned in ("C067", "prompt", "content", "messages", "reasoning", "$", *[t[:25] for t in sentences]):
            assert banned not in text, (banned, text)


# ---- the endpoint ---------------------------------------------------------------------------------------------------------------

def sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_the_endpoint_streams_and_the_plain_one_is_unchanged(profiles, monkeypatch):
    monkeypatch.setattr(client, "complete", Model(example))
    c = session_for("C067")
    r = c.post("/api/me/agent/simulate_payoff/stream", json={})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream") and r.headers["cache-control"] == "no-cache"
    events = sse(r.text)
    assert [k for k, _ in events[:-1]] == ["progress"] * (len(events) - 1) and events[-1][0] == "result"
    assert [d["seq"] for _, d in events] == list(range(len(events))) and events[0][1]["label"] == "Starting the check-up"
    assert all(set(d) <= TOP_KEYS and set(d.get("detail", {})) <= DETAIL_KEYS for _, d in events)
    plain = c.post("/api/me/agent/simulate_payoff", json={})
    assert plain.headers["X-Cache"] == "hit" and plain.json()["payoff"]["verdict"] == events[-1][1]["result"]["payoff"]["verdict"]
    again = sse(c.post("/api/me/agent/simulate_payoff/stream", json={}).text)
    assert again[0][1]["detail"]["cached"] is True and again[-1][1]["result"]["trace"][-1]["kind"] == "cache_hit"


def test_the_endpoint_checks_before_it_streams(profiles):
    c = session_for("C067")
    assert c.post("/api/me/agent/nope/stream", json={}).status_code == 404
    assert c.post("/api/me/agent/simulate_payoff/stream", json={"strategy": "F"}).status_code == 422
    assert c.post("/api/me/agent/simulate_payoff/stream", json={"customer_id": "C002"}).status_code == 422
    assert c.post("/api/me/agent/simulate_payoff/stream", content=b"[1]").status_code == 422
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).post("/api/me/agent/simulate_payoff/stream", json={}).status_code == 401
