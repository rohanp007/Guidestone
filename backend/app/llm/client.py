"""The one way agents talk to a model.

complete(agent, messages, tools=None, json_schema=None, tool_choice="auto") -> LLMResult
  - prepends the agent's reasoning flag (/think or /no_think) to the system prompt, sends the agent's temperature
  - strips <think> blocks (and never reads reasoning_content), so reasoning is neither returned nor logged
  - parses tool-call arguments with json.loads and validates them against the tool's args model
  - json_schema (a pydantic model): extracts and validates the JSON in the reply
  - a bad reply (non-JSON / invalid arguments / invalid JSON) is retried once, with a correction that names the fields at fault
  - timeout / connection error / 5xx / 429: one retry, then LLMUnavailable. 400 / 401 / 403 / 404 / 422: LLMRequestError at once
  - each call appends an entry (agent, backend, model, latency, tokens, ok; never prompt or reasoning text) to the current run's
    trace (see start_run) and to TRACE, a global debug log
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import openai
from pydantic import BaseModel, ValidationError

from ..api.serialize import to_plain
from . import config
from .prompts import BASE_SYSTEM_PROMPT
from .text import extract_json, strip_reasoning

log = logging.getLogger("guidestone.llm")
TRACE: deque[dict] = deque(maxlen=200)               # every call from every run, newest last: a debug log only
_run_trace: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar("llm_run_trace", default=None)
RETRY_BACKOFF_S = 0.5                                 # before the retry after a 429
_transport: httpx.AsyncBaseTransport | None = None    # tests inject a mock server here
_clients: dict[tuple, tuple[asyncio.AbstractEventLoop, openai.AsyncOpenAI]] = {}


class LLMError(Exception): ...
class LLMUnavailable(LLMError):
    """The server did not answer or is failing (after one retry). Callers may fall back."""
class LLMRequestError(LLMError):
    """The server rejected the request (bad model id, context too long, malformed tools, auth): a bug to fix, never a fallback case."""
class LLMResponseError(LLMError):
    """The server answered, but not with usable JSON (after one retry)."""
class ToolArgumentError(LLMResponseError):
    """The model called a tool with arguments that fail the tool's schema. `feedback` names the fields at fault."""
    def __init__(self, message: str, feedback: str = ""):
        super().__init__(message)
        self.feedback = feedback or message


def start_run() -> list[dict]:
    """Begin a new trace for the current task / request and return it. Calls in this context (and in tasks it spawns) append to it."""
    trace: list[dict] = []
    _run_trace.set(trace)
    return trace


def record(entry: dict) -> None:
    """Add a trace entry to the current run's trace and to the global debug log."""
    TRACE.append(entry)
    if (run := _run_trace.get()) is not None:
        run.append(entry)
    log.info("llm %s", entry)


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport
    _clients.clear()


async def close_clients() -> None:
    """At app shutdown."""
    for loop, c in list(_clients.values()):
        if loop is asyncio.get_running_loop():
            await c.close()
    _clients.clear()


def _field_problems(err: ValidationError) -> str:
    """'horizon_months: Input should be less than or equal to 60'. Field names and rules only, never the submitted values."""
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in err.errors())


# ---- tools ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    """A function the model may call. `fn(**args)` is usually a closure bound to the session's customer. It receives the validated
    arguments as typed values (nested models, enums and dates intact), the way the API calls formulas."""
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                   "parameters": self.args.model_json_schema()}}

    def validate(self, arguments: dict) -> BaseModel:
        try:
            return self.args.model_validate(arguments)
        except ValidationError as e:
            problems = _field_problems(e)
            raise ToolArgumentError(f"{self.name}: invalid arguments ({problems})",
                                    f"Invalid arguments for {self.name}: {problems}") from e

    def run(self, arguments: dict) -> Any:
        v = self.validate(arguments)
        return self.fn(**{f: getattr(v, f) for f in type(v).model_fields})

    async def arun(self, arguments: dict) -> Any:
        """Same, off the event loop: formulas are synchronous, and the slow ones (Monte Carlo) must not stall other requests."""
        self.validate(arguments)
        return await asyncio.to_thread(self.run, arguments)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResult:
    content: str                                  # reasoning already stripped; empty on tool calls
    tool_calls: list[ToolCall] = field(default_factory=list)
    parsed: BaseModel | None = None               # set when json_schema was given
    backend: str = ""
    model: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timings: dict[str, float] = field(default_factory=dict)   # prompt_ms, predicted_ms, prompt_n, cache_n... (llama-server only)

    def assistant_message(self) -> dict:
        """The assistant turn to append to the history before sending tool results back."""
        return _assistant_message(self.content, self.tool_calls)


def _assistant_message(content: str, calls: list[ToolCall]) -> dict:
    msg: dict = {"role": "assistant", "content": content or None}
    if calls:
        msg["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                             for c in calls]
    return msg


def tool_message(call: ToolCall, result: Any) -> dict:
    """The tool result as a message. Results go through the API's JSON-safe converter: numpy / pandas values, NaN, dates."""
    content = result if isinstance(result, str) else json.dumps(to_plain(result), allow_nan=False, separators=(",", ":"))
    return {"role": "tool", "tool_call_id": call.id, "content": content}


# ---- backends ------------------------------------------------------------------------------------------------------------------

def _client(cfg: config.AgentConfig) -> openai.AsyncOpenAI:
    """One AsyncOpenAI (one connection pool) per backend, rebuilt if the event loop or the server address changed."""
    if cfg.backend == "hosted":
        raise NotImplementedError("hosted backend is not implemented yet")
    loop, key = asyncio.get_running_loop(), (cfg.backend, cfg.base_url)
    cached = _clients.get(key)
    if cached is None or cached[0] is not loop or loop.is_closed():
        http = httpx.AsyncClient(transport=_transport) if _transport else None
        cached = _clients[key] = (loop, openai.AsyncOpenAI(base_url=key[1], api_key=config.api_key(), max_retries=0, http_client=http))
    return cached[1]


def prewarm() -> None:
    """Build the HTTP clients for every configured local backend now (SSL setup is slow enough to show in the first request).
    Call from async code, at startup."""
    for agent in config.AGENTS:
        cfg = config.agent_config(agent)
        if cfg.backend != "hosted":
            _client(cfg)


def minimize(messages: list[dict]) -> list[dict]:
    """Will strip personal data from messages before they go to a hosted backend."""
    raise NotImplementedError("minimizer is not implemented yet")


# ---- complete ------------------------------------------------------------------------------------------------------------------

@dataclass
class _Stats:
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_choice_downgraded: bool = False
    timings: dict = field(default_factory=dict)     # llama-server's own timing fields, summed over the attempts


class _BadReply(LLMResponseError):
    """A reply to retry once; `followup` is what to append to the conversation so the retry differs."""
    def __init__(self, message: str, followup: list[dict]):
        super().__init__(message)
        self.followup = followup


def _prepare(cfg: config.AgentConfig, messages: list[dict], schema: type[BaseModel] | None, tools: bool = False,
             instruct: bool = True) -> list[dict]:
    """Copy of the messages with the system prompt rebuilt as: reasoning flag, base agent prompt (when tools are offered), the caller's
    system prompt. The JSON instruction goes in a final user message, where a small model heeds it (in the system prompt it answered in
    prose and cost a retry). Fixed text first and per-call text last, so a server that caches the prompt prefix can reuse it."""
    parts = [cfg.reasoning_flag] + ([BASE_SYSTEM_PROMPT] if tools else [])
    msgs = [dict(m) for m in messages]
    has_system = bool(msgs) and msgs[0].get("role") == "system"
    if has_system:
        parts.append(msgs[0].get("content") or "")
    msgs[:1 if has_system else 0] = [{"role": "system", "content": "\n".join(parts)}]
    if schema and instruct:
        msgs.append({"role": "user", "content": "Reply with only a JSON object (no prose, no code fences) that matches this JSON Schema: "
                                                + json.dumps(schema.model_json_schema(), separators=(",", ":"))})
    return msgs


async def _request(cfg: config.AgentConfig, msgs: list[dict], tools: list[Tool] | None, tool_choice: str | dict, stats: _Stats,
                   max_tokens: int | None = None, temperature: float | None = None) -> Any:
    """One chat completion. Timeout / connection error / 5xx / 429: retried once, then LLMUnavailable. Other 4xx: LLMRequestError."""
    kwargs: dict = {"model": cfg.model, "messages": msgs, "timeout": cfg.timeout, "temperature": cfg.temperature if temperature is None else temperature,
              "max_tokens": max_tokens or cfg.max_tokens}
    if tools:
        if tool_choice == "required" and not cfg.supports_required_tool_choice:
            tool_choice, stats.tool_choice_downgraded = "auto", True   # would hang llama-server; callers rely on validation + fallback plans
        kwargs["tools"], kwargs["tool_choice"] = [t.spec() for t in tools], tool_choice
    for attempt in (1, 2):
        t0 = time.perf_counter()
        try:
            resp = await _client(cfg).chat.completions.create(**kwargs)
        except (openai.APIConnectionError, openai.InternalServerError, openai.RateLimitError) as e:    # includes APITimeoutError
            stats.latency_ms += (time.perf_counter() - t0) * 1000
            if attempt == 2:
                raise LLMUnavailable(f"{cfg.backend} unavailable ({type(e).__name__})") from None
            if isinstance(e, openai.RateLimitError):
                await asyncio.sleep(RETRY_BACKOFF_S)
            continue
        except openai.APIError as e:                         # 400 / 401 / 403 / 404 / 422 ...: our request is wrong, retrying cannot help
            stats.latency_ms += (time.perf_counter() - t0) * 1000
            status = getattr(e, "status_code", None)
            raise LLMRequestError(f"{cfg.backend} rejected the request ({type(e).__name__}{f' {status}' if status else ''})") from None
        stats.latency_ms += (time.perf_counter() - t0) * 1000
        usage = getattr(resp, "usage", None)
        stats.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        stats.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        for k, v in ((getattr(resp, "model_extra", None) or {}).get("timings") or {}).items():
            if isinstance(v, (int, float)) and k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "cache_n"):
                stats.timings[k] = round(stats.timings.get(k, 0) + v, 1)
        return resp
    raise AssertionError("unreachable")


def _parse(resp: Any, tools: list[Tool] | None, schema: type[BaseModel] | None) -> tuple[str, list[ToolCall], BaseModel | None]:
    msg = resp.choices[0].message
    content = strip_reasoning(msg.content)
    by_name = {t.name: t for t in tools or []}
    calls: list[ToolCall] = []
    for c in msg.tool_calls or []:
        try:
            args = json.loads(c.function.arguments or "{}")
            if not isinstance(args, dict): raise ValueError
        except ValueError:
            raise _BadReply(f"tool call {c.function.name!r} had non-JSON arguments", [
                {"role": "assistant", "content": content or None},
                {"role": "user", "content": f"Your call to {c.function.name} did not have a valid JSON object as its arguments. "
                                            "Call it again with arguments as a JSON object."}]) from None
        calls.append(ToolCall(c.id, c.function.name, args))
    if calls and tools:
        problems = []
        for call in calls:
            tool = by_name.get(call.name)
            if tool is None:
                problems.append(f"No tool named {call.name}. Available: {', '.join(by_name)}.")
                continue
            try:
                tool.validate(call.arguments)
                problems.append("")
            except ToolArgumentError as e:
                problems.append(e.feedback)
        if any(problems):
            fix = " ".join(p for p in problems if p)
            raise _BadReply("tool call failed validation: " + fix, [_assistant_message(content, calls)] + [
                {"role": "tool", "tool_call_id": c.id, "content": (p or "Not run, because another call in the same turn was invalid.")
                 + " Call the tool again with corrected arguments."} for c, p in zip(calls, problems)])
    parsed = None
    if schema and not calls:
        try:
            parsed = schema.model_validate(extract_json(content))
        except ValidationError as e:
            raise _BadReply(f"reply is not valid {schema.__name__} JSON", [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"That JSON does not match the schema ({_field_problems(e)}). Reply again with only the JSON object."}]) from None
        except ValueError:
            raise _BadReply(f"reply is not valid {schema.__name__} JSON", [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "That was not valid JSON. Reply again with only the JSON object."}]) from None
    return content, calls, parsed


async def complete(agent: str, messages: list[dict], tools: list[Tool] | None = None, json_schema: type[BaseModel] | None = None,
                   tool_choice: str | dict = "auto", max_tokens: int | None = None,
                   schema_in_prompt: bool = True, temperature: float | None = None) -> LLMResult:
    """tool_choice: "auto", "required" (must call a tool; downgraded to "auto" on llama-server, which the trace records) or "none".
    temperature overrides the agent's default for this call (used by retries, which at temperature 0 would repeat themselves).
    max_tokens overrides the agent's default for this call (e.g. 120 for a one-sentence summary).
    schema_in_prompt=False: validate against json_schema but do not append the "match this JSON Schema" message, for callers whose own
    prompt already describes the format (shown to a small model, a bare schema invites the emptiest valid answer, e.g. {"steps":[]})."""
    cfg = config.agent_config(agent)
    msgs = _prepare(cfg, messages, json_schema, bool(tools), schema_in_prompt)
    stats, ok = _Stats(), False
    try:
        for attempt in (1, 2):
            resp = await _request(cfg, msgs, tools, tool_choice, stats, max_tokens, temperature)
            try:
                content, calls, parsed = _parse(resp, tools, json_schema)
            except _BadReply as bad:
                if attempt == 2:
                    raise LLMResponseError(str(bad)) from None
                msgs = msgs + bad.followup
                continue
            ok = True
            return LLMResult(content, calls, parsed, cfg.backend, cfg.model, stats.latency_ms, stats.prompt_tokens, stats.completion_tokens, dict(stats.timings))
        raise AssertionError("unreachable")
    finally:
        entry = {"agent": agent, "backend": cfg.backend, "model": cfg.model, "latency_ms": round(stats.latency_ms, 1),
                 "prompt_tokens": stats.prompt_tokens, "completion_tokens": stats.completion_tokens, "ok": ok,
                 "tool_choice_downgraded": stats.tool_choice_downgraded}
        record(entry)
