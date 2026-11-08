"""LLM client against a mocked OpenAI-compatible server (httpx.MockTransport). No real model is called."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.llm import client, config, health
from app.llm.client import LLMRequestError, LLMResponseError, LLMUnavailable, Tool, ToolArgumentError, complete


class Summary(BaseModel):
    summary: str


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    horizon_months: int = Field(12, ge=1, le=60)


def reply(content: str | None = "", tool_calls: list | None = None, reasoning: str = "") -> httpx.Response:
    msg: dict = {"role": "assistant", "content": content}
    if reasoning: msg["reasoning_content"] = reasoning
    if tool_calls: msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                     "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


def call(name: str, arguments: str, id: str = "call_1") -> dict:
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.fixture
def server():
    """Install a mock server; `server.handler` takes the parsed request body and returns a Response (or raises)."""
    state = type("S", (), {"requests": [], "handler": None})()

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state.requests.append(body)
        return state.handler(body)

    client.set_transport(httpx.MockTransport(route))
    client.TRACE.clear()
    client.RETRY_BACKOFF_S = 0
    yield state
    client.set_transport(None)
    client.RETRY_BACKOFF_S = 0.5


def run(coro):
    return asyncio.run(coro)


def test_reasoning_stripped_and_flag_prepended(server):
    server.handler = lambda b: reply("<think>secret plan</think>Hello there", reasoning="more secret")
    r = run(complete("researcher", [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]))
    assert r.content == "Hello there" and "secret" not in r.content
    sent = server.requests[0]["messages"]
    assert sent[0]["content"].startswith("/no_think") and "Be brief." in sent[0]["content"]       # researcher: reasoning off
    run(complete("advisor", [{"role": "user", "content": "hi"}]))
    assert server.requests[1]["messages"][0] == {"role": "system", "content": "/think"}             # advisor: on, no system prompt given


def test_strip_reasoning_variants():
    s = client.strip_reasoning
    assert s("<think>a</think>b") == "b" and s("a plan</think>answer") == "answer" and s("answer<think>unfinished") == "answer"
    assert s(None) == ""


def test_request_parameters_and_result_metadata(server):
    server.handler = lambda b: reply("", [call("forecast", "{}")])
    tool = Tool("forecast", "d", Args, lambda **kw: kw)
    msgs = [{"role": "user", "content": "go"}]
    r = run(complete("orchestrator", msgs, tools=[tool], tool_choice="required"))
    req = server.requests[0]
    assert req["tool_choice"] == "auto"                                   # "required" hangs llama-server, so it is downgraded there
    assert req["temperature"] == 0 and req["max_tokens"] == 512
    assert "output only the tool call" in req["messages"][0]["content"]
    assert (r.backend, r.model) == ("llamacpp", "nemotron-nano-4b-nothink")
    run(complete("advisor", msgs, tools=[tool], tool_choice="required"))
    assert server.requests[1]["tool_choice"] == "required" and server.requests[1]["max_tokens"] == 1024
    run(complete("advisor", msgs))
    assert "output only the tool call" not in server.requests[2]["messages"][0]["content"]


def test_per_agent_urls_and_clients(server, monkeypatch):
    seen = []
    server.handler = lambda b: reply("ok")
    client.set_transport(httpx.MockTransport(lambda r: (seen.append(str(r.url)), reply("ok"))[1]))

    async def go():
        for agent in ("orchestrator", "researcher", "presenter", "advisor"):
            await complete(agent, [{"role": "user", "content": "go"}])
        return len(client._clients)

    assert run(go()) == 2                                                  # one client per (backend, base_url)
    assert seen == ["http://localhost:1235/v1/chat/completions"] * 3 + ["http://localhost:1234/v1/chat/completions"]
    monkeypatch.setenv("LLM_BASE_URL", "http://example:9/v1")
    monkeypatch.setenv("LLM_ADVISOR_BASE_URL", "http://advisor:1/v1")
    monkeypatch.setenv("LLM_ORCHESTRATOR_MAX_TOKENS", "99")
    assert config.agent_config("advisor").base_url == "http://advisor:1/v1"
    assert config.agent_config("orchestrator").base_url == "http://example:9/v1"           # no URL of its own: LLM_BASE_URL
    monkeypatch.setenv("LLM_ORCHESTRATOR_BASE_URL", "http://orch:2/v1")
    assert config.agent_config("orchestrator").base_url == "http://orch:2/v1"
    assert config.agent_config("orchestrator").max_tokens == 99
    monkeypatch.setenv("LLM_RESEARCHER_BACKEND", "lmstudio")
    assert config.agent_config("researcher").base_url == "http://example:9/v1"


def test_trace_has_no_prompt_or_reasoning(server):
    server.handler = lambda b: reply("<think>SECRET-REASONING</think>SECRET-ANSWER")
    run(complete("advisor", [{"role": "user", "content": "SECRET-PROMPT"}]))
    entry = client.TRACE[-1]
    assert set(entry) == {"agent", "backend", "model", "latency_ms", "prompt_tokens", "completion_tokens", "ok",
                         "tool_choice_downgraded", "temperature"}
    assert entry["ok"] and entry["agent"] == "advisor" and entry["prompt_tokens"] == 10 and entry["completion_tokens"] == 5
    assert "SECRET" not in json.dumps(entry)


def test_tool_call_arguments_parsed(server):
    server.handler = lambda b: reply("", [call("forecast", '{"horizon_months": 6}')])
    tool = Tool("forecast", "d", Args, lambda **kw: kw)
    r = run(complete("orchestrator", [{"role": "user", "content": "go"}], tools=[tool]))
    assert r.tool_calls[0].arguments == {"horizon_months": 6} and r.tool_calls[0].id == "call_1"
    assert server.requests[0]["tools"][0]["function"]["name"] == "forecast"
    assert r.assistant_message()["tool_calls"][0]["function"]["arguments"] == '{"horizon_months": 6}'
    assert client.tool_message(r.tool_calls[0], {"a": 1}) == {"role": "tool", "tool_call_id": "call_1", "content": '{"a":1}'}


def test_bad_tool_arguments_rejected():
    tool = Tool("forecast", "d", Args, lambda **kw: kw)
    assert tool.run({"horizon_months": 6}) == {"horizon_months": 6}
    for bad in ({"horizon_months": 0}, {"horizon_months": "soon"}, {"horizon_months": 6, "customer_id": "C999"}):
        with pytest.raises(ToolArgumentError):
            tool.run(bad)


def test_non_json_tool_arguments_get_a_correction(server):
    outcomes = iter([reply("", [call("forecast", "{not json")]), reply("", [call("forecast", '{"horizon_months": 3}')])])
    server.handler = lambda b: next(outcomes)
    tool = Tool("forecast", "d", Args, lambda **kw: kw)
    r = run(complete("orchestrator", [{"role": "user", "content": "go"}], tools=[tool]))
    assert r.tool_calls[0].arguments == {"horizon_months": 3}
    assert server.requests[1]["messages"] != server.requests[0]["messages"]                  # not the same request again
    assert "valid JSON" in server.requests[1]["messages"][-1]["content"]

    server.handler = lambda b: reply("", [call("forecast", "{not json")])
    with pytest.raises(LLMResponseError):
        run(complete("orchestrator", [{"role": "user", "content": "go"}], tools=[tool]))
    assert client.TRACE[-1]["ok"] is False


def test_bad_tool_arguments_give_a_correction_naming_fields(server):
    outcomes = iter([reply("", [call("forecast", '{"horizon_months": 99, "customer_id": "C9"}')]),
                     reply("", [call("forecast", '{"horizon_months": 24}')])])
    server.handler = lambda b: next(outcomes)
    tool = Tool("forecast", "d", Args, lambda **kw: kw)
    r = run(complete("orchestrator", [{"role": "user", "content": "go"}], tools=[tool]))
    assert r.tool_calls[0].arguments == {"horizon_months": 24}
    followup = server.requests[1]["messages"][-2:]
    assert followup[0]["tool_calls"][0]["id"] == "call_1"
    assert followup[1]["role"] == "tool" and followup[1]["tool_call_id"] == "call_1"
    fb = followup[1]["content"]
    assert "horizon_months" in fb and "less than or equal to 60" in fb and "customer_id" in fb
    assert "99" not in fb and "C9" not in fb                                                  # field names and rules, never values

    with pytest.raises(ToolArgumentError) as e:
        tool.validate({"horizon_months": 0})
    assert "horizon_months" in e.value.feedback

    server.handler = lambda b: reply("", [call("forecast", '{"horizon_months": 99}')])         # still wrong after the retry
    with pytest.raises(LLMResponseError):
        run(complete("orchestrator", [{"role": "user", "content": "go"}], tools=[tool]))


def test_tool_result_with_numpy_serializes():
    result = {"a": np.float64(1.5), "b": np.arange(3), "c": pd.Timestamp("2026-01-02"), "d": float("nan"), "e": np.int64(7)}
    msg = client.tool_message(client.ToolCall("call_1", "t", {}), result)
    assert json.loads(msg["content"]) == {"a": 1.5, "b": [0, 1, 2], "c": "2026-01-02T00:00:00", "d": None, "e": 7}


def test_tool_receives_typed_values():
    seen = {}

    class Item(BaseModel):
        kind: str

    class A(BaseModel):
        items: list[Item] = []

    Tool("t", "d", A, lambda **kw: seen.update(kw)).run({"items": [{"kind": "x"}]})
    assert isinstance(seen["items"][0], Item)                                                 # nested models stay models, not dicts


def test_session_tool_requires_summary_variant():
    from app.llm.tools import session_tool
    with pytest.raises(ValueError):
        session_tool(None, "forecast_liquid")
    assert session_tool(None, "forecast_liquid_summary").name == "forecast_liquid_summary"


def test_400_raises_immediately_without_retry(server):
    for status in (400, 401, 404):
        server.requests.clear()
        server.handler = lambda b, status=status: httpx.Response(status, json={"error": {"message": "no"}})
        with pytest.raises(LLMRequestError):
            run(complete("orchestrator", [{"role": "user", "content": "go"}]))
        assert len(server.requests) == 1
    assert not issubclass(LLMRequestError, LLMUnavailable)


def test_429_and_5xx_retry_once(server):
    for status in (429, 503):
        server.requests.clear()
        server.handler = lambda b, status=status: httpx.Response(status, json={"error": {"message": "busy"}})
        with pytest.raises(LLMUnavailable):
            run(complete("orchestrator", [{"role": "user", "content": "go"}]))
        assert len(server.requests) == 2


def test_failure_records_latency(server):
    def slow(body):
        time.sleep(0.02)
        raise httpx.ReadTimeout("slow")
    server.handler = slow
    with pytest.raises(LLMUnavailable):
        run(complete("orchestrator", [{"role": "user", "content": "go"}]))
    assert client.TRACE[-1]["latency_ms"] >= 30 and client.TRACE[-1]["ok"] is False


def test_concurrent_runs_keep_separate_traces(server):
    server.handler = lambda b: reply("ok")

    async def one_run(agent: str, n: int):
        trace = client.start_run()
        for _ in range(n):
            await complete(agent, [{"role": "user", "content": "go"}])
            await asyncio.sleep(0)
        return trace

    async def both():
        return await asyncio.gather(one_run("researcher", 3), one_run("advisor", 2))

    a, b = run(both())
    assert [e["agent"] for e in a] == ["researcher"] * 3 and [e["agent"] for e in b] == ["advisor"] * 2
    assert len(client.TRACE) == 5                                                              # the global deque still sees everything


def test_reuses_one_http_client(server):
    server.handler = lambda b: reply("ok")

    async def twice():
        await complete("researcher", [{"role": "user", "content": "go"}])
        first = next(iter(client._clients.values()))[1]
        await complete("presenter", [{"role": "user", "content": "go"}])              # same backend and URL as the researcher
        assert next(iter(client._clients.values()))[1] is first and len(client._clients) == 1
        await client.close_clients()

    run(twice())


def test_json_schema_retries_once(server):
    replies = iter(["Sure! here you go", '```json\n{"summary": "fine"}\n```'])
    server.handler = lambda b: reply(next(replies))
    r = run(complete("advisor", [{"role": "user", "content": "go"}], json_schema=Summary))
    assert r.parsed == Summary(summary="fine") and len(server.requests) == 2
    assert server.requests[1]["messages"][-1]["role"] == "user"                       # the correction
    assert r.prompt_tokens == 20                                                      # both attempts counted


def test_json_schema_fails_after_one_retry(server):
    server.handler = lambda b: reply('{"wrong": 1}')
    with pytest.raises(LLMResponseError):
        run(complete("advisor", [{"role": "user", "content": "go"}], json_schema=Summary))
    assert len(server.requests) == 2 and client.TRACE[-1]["ok"] is False


def test_timeout_retries_once_then_unavailable(server):
    def boom(body): raise httpx.ReadTimeout("slow")
    server.handler = boom
    with pytest.raises(LLMUnavailable):
        run(complete("orchestrator", [{"role": "user", "content": "go"}]))
    assert len(server.requests) == 2 and client.TRACE[-1]["ok"] is False


def test_recovers_after_one_error(server):
    outcomes = iter([httpx.Response(500, json={"error": "boom"}), reply("ok")])
    server.handler = lambda b: next(outcomes)
    assert run(complete("researcher", [{"role": "user", "content": "go"}])).content == "ok"


def test_hosted_and_minimizer_are_stubs(server, monkeypatch):
    monkeypatch.setenv("LLM_PRESENTER_BACKEND", "hosted")
    with pytest.raises(NotImplementedError):
        run(complete("presenter", [{"role": "user", "content": "go"}]))
    with pytest.raises(NotImplementedError):
        client.minimize([])


def test_agent_config_defaults_and_env(monkeypatch):
    on = {a: config.agent_config(a).reasoning for a in config.AGENTS}
    assert on == {"orchestrator": False, "researcher": False, "presenter": False, "advisor": True}
    monkeypatch.setenv("LLM_RESEARCHER_REASONING", "true")
    monkeypatch.setenv("LLM_RESEARCHER_MODEL", "other")
    monkeypatch.setenv("LLM_RESEARCHER_TIMEOUT", "5")
    c = config.agent_config("researcher")
    assert (c.reasoning, c.model, c.timeout, c.reasoning_flag) == (True, "other", 5.0, "/think")
    with pytest.raises(ValueError):
        config.agent_config("nobody")


def test_health_probe_and_cache():
    hits = []

    def route(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        model = "nvidia/nemotron-3-nano-4b" if request.url.port == 1234 else "nemotron-nano-4b-nothink"
        return httpx.Response(200, json={"data": [{"id": model}]})

    health.set_transport(httpx.MockTransport(route))
    try:
        assert run(health.llm_health()) == {"reachable": True, "models_ready": True}
        run(health.llm_health())
        assert sorted(hits) == ["http://localhost:1234/v1/models", "http://localhost:1235/v1/models"]   # one probe per server, cached 30 s

        def half_down(request: httpx.Request) -> httpx.Response:
            if request.url.port == 1235: raise httpx.ConnectError("down")
            return route(request)

        health.set_transport(httpx.MockTransport(half_down))
        assert run(health.llm_health()) == {"reachable": False, "models_ready": False}
    finally:
        health.set_transport(None)


def test_slim_tool_schema_and_fact_sheet():
    from datetime import date
    from app.data.loader import load_profiles
    from app.formulas import forecast
    from app.llm.facts import forecast_facts
    from app.llm.tools import session_tool

    profile = next(iter(load_profiles().values()))
    tool = session_tool(profile, "forecast_liquid_summary")
    assert list(tool.spec()["function"]["parameters"]["properties"]) == ["horizon_months"]      # no seed, paths, nested what-ifs
    with pytest.raises(ToolArgumentError) as e:
        tool.run({"horizon_months": 12, "seed": 5})
    assert "seed" in e.value.feedback
    result = tool.run({"horizon_months": 6})
    assert result.horizon_months == 6
    facts = forecast_facts(result)
    assert len(json.dumps(facts)) < 900 and len(facts["next_events"]) <= 3
    assert {"start_balance", "monthly_gain", "low_point", "cushion", "chance_of_overdraft"} <= set(facts)
    json.dumps(facts, allow_nan=False)


def test_downgrade_is_traced_and_json_instruction_comes_last(server):
    server.handler = lambda b: reply('{"summary": "x"}')
    tool = Tool("forecast", "d", Args, lambda **kw: kw)
    run(complete("orchestrator", [{"role": "system", "content": "S"}, {"role": "user", "content": "go"}], tools=[tool],
                 tool_choice="required", json_schema=Summary, max_tokens=77))
    req = server.requests[0]
    assert client.TRACE[-1]["tool_choice_downgraded"] is True and req["max_tokens"] == 77
    assert req["messages"][0]["content"] == "/no_think\nWhen you call a tool, output only the tool call, with no explanation.\nS"
    assert req["messages"][-1]["role"] == "user" and "JSON" in req["messages"][-1]["content"]
    run(complete("advisor", [{"role": "user", "content": "go"}], tools=[tool], tool_choice="required"))
    assert client.TRACE[-1]["tool_choice_downgraded"] is False


def test_llama_server_timings_are_captured(server):
    r = reply("hi")
    body = json.loads(r.content); body["timings"] = {"prompt_ms": 12.5, "predicted_ms": 30.0, "cache_n": 7, "junk": "x"}
    server.handler = lambda b: httpx.Response(200, json=body)
    out = run(complete("researcher", [{"role": "user", "content": "go"}]))
    assert out.timings == {"prompt_ms": 12.5, "predicted_ms": 30.0, "cache_n": 7}
