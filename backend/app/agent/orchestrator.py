"""Orchestrator v1: one event, dashboard_open.

precompute (parallel, no LLM) -> facts (labels set in code) -> plan (one structured LLM call, validated in code, default plan on any
failure) -> execute (parallel) -> grounded explanation (template on failure) -> result.

Rules: the customer is whoever the session says (`profile` is an argument, never something the model names); numbers come from
formulas; judgments are labels computed in code; the trace holds tool names, codes, timings and counts, never prompt or model text.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..api.serialize import to_plain
from ..data import loader
from ..data.models import Profile
from ..formulas.registry import FORMULAS
from ..llm import client, grounding
from ..llm.facts import forecast_facts, model_view, placeholders
from ..llm.prompts import EXPLAIN_PROMPT
from ..llm.tools import Tool, session_tool
from . import advisor, buy_advisor, cache, payoff_advisor, researcher
from .events import EventSpec, get_event
from . import stream as stream_mod
from ..research.sanitize import sanitize
from .models import MAX_STEPS, Advice, AnalysisResult, BuyAnalysis, PayoffAnalysis, GroundingSummary, HeadlineSummary, Plan, PlanStep, TraceStep
from .prompts import ORCH_PLAN_V1, PROMPT_VERSION, TOOL_HINTS

log = logging.getLogger("guidestone.agent")
RUN_TIMEOUT_S = 30.0
TRANSIENT_TTL_S = 60.0           # a result whose advice is a stand-in for a model that was slow or down is kept this long, not for the session
RERUN_DELAY_S = 5.0              # ...and one background rerun starts this long after it was served
TRANSIENT_REASONS = {"budget_exceeded", "llm_unavailable", "run_timeout"}
_reruns: set[asyncio.Task] = set()
PLAN_MAX_TOKENS = 200
EXPLAIN_MAX_TOKENS = 200


@dataclass
class _State:
    """Everything a run has produced so far, so a timeout can still return the partial result."""
    spec: EventSpec
    profile: Profile
    inputs: dict
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trace: list[TraceStep] = field(default_factory=list)
    compute: dict[str, Any] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)          # (tool, canonical args) already computed
    raw: dict[str, Any] = field(default_factory=dict)    # formula results as objects, for the fact sheet
    facts: dict | None = None
    summary: HeadlineSummary | None = None
    rules: advisor.Inputs | None = None                  # the decision rules' output (waterfall, candidate moves, verdict)
    advice: Advice | None = None
    payoff: PayoffAnalysis | None = None
    buy: BuyAnalysis | None = None
    buy_parts: tuple | None = None                       # (plans, facts) once known, so a timeout can still answer from them
    pf: payoff_advisor.PayoffFacts | None = None         # the payoff event's figures, once known (a timeout can still answer from them)
    pf_parts: tuple | None = None                        # (summary, chosen strategy, verdict, simulation)
    degraded: bool = False
    llm_trace: list[dict] = field(default_factory=list)
    stream: stream_mod.RunStream | None = None           # progress events for this run (the /stream endpoint); the trace feeds it

    def __post_init__(self):
        if self.stream is not None:
            self.trace = stream_mod.TraceList(self.stream)


def _say(st: _State, agent: str, phase: str, label: str, **detail) -> None:
    """A progress event for a step that is about to start (finished steps are reported by the trace itself)."""
    if st.stream is not None:
        st.stream.progress(agent, phase, label, **detail)


def _canon(tool: str, args: dict) -> str:
    return f"{tool}:{json.dumps(args, sort_keys=True, default=str)}"


def _store(st: _State, tool: str, value: Any) -> None:
    key, n = tool, 1
    while key in st.compute:
        n += 1
        key = f"{tool}#{n}"
    st.compute[key] = to_plain(value)


async def _timed(st: _State, kind: str, name: str, coro, **detail):
    """Await `coro`, adding a trace step. Returns (value, ok)."""
    t0 = time.perf_counter()
    try:
        value, ok = await coro, True
        extra = {}
    except Exception as e:                      # recorded by type only; the caller decides whether this is fatal
        value, ok, extra = e, False, {"error": type(e).__name__}
    st.trace.append(TraceStep(kind=kind, name=name, ms=round((time.perf_counter() - t0) * 1000, 1), ok=ok, detail={**detail, **extra}))
    return value, ok


# ---- precompute ----------------------------------------------------------------------------------------------------------------

async def precompute(st: _State) -> None:
    """Every formula the event always needs. A failure here is a bug, not a model problem: the run fails."""
    tools = [(s, session_tool(st.profile, s.tool)) for s in st.spec.precompute]
    _say(st, "compute", "start", stream_mod.LABELS["calc"][0])

    async def one(step: PlanStep, tool: Tool):
        value, ok = await _timed(st, "precompute", step.tool, tool.arun(step.args))
        if not ok:
            raise value
        return step, tool, value

    for step, tool, value in await asyncio.gather(*(one(s, t) for s, t in tools)):
        st.seen.add(_canon(step.tool, tool.validate(step.args).model_dump()))
        st.compute[step.tool] = to_plain(value)
        st.raw[step.tool] = value
    _say(st, "compute", "done", stream_mod.LABELS["calc"][1])


# ---- facts ---------------------------------------------------------------------------------------------------------------------

def build_facts(st: _State) -> dict:
    """The forecast fact sheet (labels, overall_status) plus the dashboard facts. The model sees llm.facts.model_view(facts), not this."""
    facts = forecast_facts(st.raw["forecast_liquid_summary"])
    p2p = st.raw["paycheck_to_paycheck"]
    facts["paycheck_to_paycheck"] = "yes" if p2p.flag else "no"
    facts["employment"] = st.profile.employment
    return facts


# ---- plan ----------------------------------------------------------------------------------------------------------------------

def _describe(tool: Tool) -> str:
    props = tool.args.model_json_schema().get("properties", {})
    args = []
    for k, v in props.items():
        rng = ", ".join(f"{b} {v[b]}" for b in ("minimum", "maximum") if b in v)
        args.append(f"{k} ({v.get('type', 'any')}{', ' + rng if rng else ''}; default {v.get('default')})")
    return f"- {tool.name}: {tool.description} {TOOL_HINTS.get(tool.name, '')} Args: {'; '.join(args) or 'none'}."


def _planner_messages(st: _State, tools: list[Tool]) -> list[dict]:
    f = st.facts
    context = (f"Customer: cushion_status={f['cushion_status']}, trend={f['trend']}, overdraft_status={f['overdraft_status']}, "
               f"paycheck_to_paycheck={f['paycheck_to_paycheck']}, employment={f['employment']}.")
    return [{"role": "system", "content": ORCH_PLAN_V1 + "\n\nTools:\n" + "\n".join(_describe(t) for t in tools)},
            {"role": "user", "content": context}]


def validate_plan(st: _State, plan: Plan) -> tuple[list[tuple[PlanStep, Tool, dict]], list[tuple[str, str]]]:
    """(accepted steps with their tool and validated args, rejected as (tool name, code)). Codes: not_allowed, bad_args, duplicate,
    too_many. Only tool names the registry knows are ever recorded; anything else is 'unknown'."""
    allowed = st.spec.tools_for(st.profile)
    seen, ok, rejected = set(st.seen), [], []
    for step in plan.steps:
        name = step.tool if step.tool in FORMULAS else "unknown"
        if len(ok) >= MAX_STEPS:
            rejected.append((name, "too_many")); continue
        if step.tool not in allowed:
            rejected.append((name, "not_allowed")); continue
        tool = session_tool(st.profile, step.tool)
        try:
            args = tool.validate(step.args).model_dump()
        except client.ToolArgumentError:
            rejected.append((name, "bad_args")); continue
        if _canon(step.tool, args) in seen:
            rejected.append((name, "duplicate")); continue
        seen.add(_canon(step.tool, args))
        ok.append((step, tool, args))
    return ok, rejected


async def plan_steps(st: _State) -> list[tuple[PlanStep, Tool, dict]]:
    """One structured LLM call. Never raises because planning failed: the event's default plan takes over."""
    allowed = st.spec.tools_for(st.profile)
    t0 = time.perf_counter()
    _say(st, "orchestrator", "start", stream_mod.LABELS["plan"][0])
    try:
        res = await client.complete("orchestrator", _planner_messages(st, [session_tool(st.profile, t) for t in allowed]),
                                    json_schema=Plan, max_tokens=PLAN_MAX_TOKENS, schema_in_prompt=False)
        steps, rejected = validate_plan(st, res.parsed)
        st.trace.append(TraceStep(kind="plan", name="orchestrator", ms=round((time.perf_counter() - t0) * 1000, 1),
                                  detail={"steps": len(steps), "rejected": len(rejected), "proposed": len(res.parsed.steps)}))
        st.trace += [TraceStep(kind="plan_rejected", name=n, ok=False, detail={"code": c}) for n, c in rejected]
        if rejected:
            st.degraded = True
        return steps
    except client.LLMError as e:
        st.degraded = True
        st.trace.append(TraceStep(kind="fallback", name="plan", ms=round((time.perf_counter() - t0) * 1000, 1), ok=False,
                                  detail={"reason": type(e).__name__, "default_steps": len(st.spec.default_plan(st.profile))}))
        steps, _ = validate_plan(st, Plan(steps=st.spec.default_plan(st.profile)))
        return steps


# ---- execute -------------------------------------------------------------------------------------------------------------------

async def execute(st: _State, steps: list[tuple[PlanStep, Tool, dict]]) -> None:
    """Run the accepted steps in parallel. A failed step is recorded and skipped."""
    async def one(step: PlanStep, tool: Tool, args: dict):
        return step, await _timed(st, "tool", step.tool, tool.arun(args), reason_len=len(step.reason))

    for step, (value, ok) in await asyncio.gather(*(one(*s) for s in steps)):
        if ok:
            _store(st, step.tool, value)
        else:
            st.degraded = True


# ---- explain -------------------------------------------------------------------------------------------------------------------

def _template_summary(st: _State) -> HeadlineSummary:
    text = grounding.template(st.facts)
    rep = grounding.check(text, st.facts, judgments=True, style=True)
    return HeadlineSummary(text=text, source="template", grounding=GroundingSummary(ok=rep.ok, checked=len(rep.numbers), retried=False))


async def explain(st: _State) -> None:
    _say(st, "presenter", "start", stream_mod.LABELS["summary"][0])
    messages = [{"role": "system", "content": EXPLAIN_PROMPT}, {"role": "user", "content": model_view(st.facts)}]
    t0 = time.perf_counter()
    try:
        out = await grounding.explain("presenter", messages, st.facts, context_numbers=(12, 24), placeholders=placeholders(st.facts),
                                      max_tokens=EXPLAIN_MAX_TOKENS)
        st.summary = HeadlineSummary(text=out.text, source="template" if out.used_template else "model",
                                     grounding=GroundingSummary(ok=out.report.ok, checked=len(out.report.numbers), retried=out.retried))
        st.trace.append(TraceStep(kind="explain", name="presenter", ms=round((time.perf_counter() - t0) * 1000, 1),
                                  detail={"retried": out.retried, "template": out.used_template, "checked": len(out.report.numbers)}))
        if out.used_template:
            st.degraded = True
    except client.LLMError as e:
        st.degraded = True
        st.summary = _template_summary(st)
        st.trace.append(TraceStep(kind="fallback", name="explain", ms=round((time.perf_counter() - t0) * 1000, 1), ok=False,
                                  detail={"reason": type(e).__name__}))


# ---- rules and advice ----------------------------------------------------------------------------------------------------------

async def run_rules(st: _State) -> None:
    """The decision rules (waterfall, candidate moves, verdict): formulas only, no model. A failure is a bug and fails the run."""
    inputs, ok = await _timed(st, "rules", "decision_rules", asyncio.to_thread(advisor.prepare, st.profile, st.facts))
    if not ok:
        raise inputs
    st.rules = inputs
    st.compute["priority_waterfall"] = to_plain(inputs.waterfall)
    st.compute["candidate_moves"] = to_plain(inputs.moves)
    st.trace[-1].detail |= {"verdict": inputs.verdict, "candidates": len(inputs.candidates)}


async def advise(st: _State) -> None:
    _say(st, "advisor", "start", stream_mod.LABELS["advice"][0])
    t0 = time.perf_counter()
    try:
        st.advice = await advisor.advise(st.rules, st.facts)
        d = st.advice.diagnostics
        detail = {"source": st.advice.source, "moves": len(st.advice.moves), "retried": d.retried, "first_try_ok": d.first_try_ok,
                  "template_moves": d.template_moves, "reasons": ",".join(d.final_reasons or d.first_reasons)[:39]}
        st.trace.append(TraceStep(kind="advise", name="advisor", ms=round((time.perf_counter() - t0) * 1000, 1), detail=detail))
        if st.advice.source != "model" and d.first_reasons != ["single_move"]:      # a deliberate template, not a fallback
            st.degraded = True
    except Exception as e:                                  # the advisor must never take the dashboard down
        st.degraded = True
        st.advice = advisor.template_advice(st.rules)
        st.trace.append(TraceStep(kind="fallback", name="advise", ms=round((time.perf_counter() - t0) * 1000, 1), ok=False,
                                  detail={"reason": type(e).__name__}))


# ---- the payoff event ----------------------------------------------------------------------------------------------------------

def _payoff_analysis(st: _State, summary, chosen, verdict, simulation, answer) -> PayoffAnalysis:
    rec = summary.recommended
    return PayoffAnalysis(verdict=verdict, recommended=rec, strategy_code=chosen.code if chosen else None,
                          strategy_title=payoff_advisor.STRATEGY_TITLE[chosen.code] if chosen else None, extra_monthly=summary.extra_monthly,
                          lump_sum=summary.lump_sum, safe_lump_sum=summary.safe_lump_sum, comparison=summary.strategies, simulation=simulation,
                          answer=answer)


async def _pipeline_payoff(st: _State) -> None:
    """The fixed plan: payoff_strategies_summary, then simulate_payoff for the recommended strategy (or the caller's), then the answer.
    No debt: a deterministic result and no model call."""
    st.llm_trace = client.start_run()
    given = {k: st.inputs[k] for k in ("extra_monthly", "lump_sum") if st.inputs.get(k) is not None}
    tool = session_tool(st.profile, "payoff_strategies_summary")
    _say(st, "compute", "start", stream_mod.LABELS["compare"][0])
    summary, ok = await _timed(st, "precompute", "payoff_strategies_summary", tool.arun(given))
    if not ok:
        raise summary
    if summary.no_debt and summary.excluded_mortgages:
        # only a mortgage is owed: not "no debt". Run the plan with it in scope so the answer can compare its rate with savings.
        given["include_mortgage"] = True
        summary, ok = await _timed(st, "precompute", "payoff_strategies_summary", tool.arun(given))
        if not ok:
            raise summary
    st.compute["payoff_strategies_summary"] = to_plain(summary)
    if summary.no_debt:
        st.facts = {}
        st.payoff = _payoff_analysis(st, summary, None, "no_debt", None, payoff_advisor.no_debt_answer())
        return
    code = st.inputs.get("strategy") or summary.recommended.code or "A"       # the recommendation is code's: payoff_priority
    chosen = next(s for s in summary.strategies if s.code == code)
    verdict = payoff_advisor.verdict_for(summary, chosen)
    # the full result (schedule and month-end bands) is for the screen, not a model: so the registry, not an agent tool
    formula = FORMULAS["simulate_payoff"]
    _say(st, "compute", "start", stream_mod.LABELS["simulate"][0])
    sim_args = formula.args.model_validate({"strategy": code, **given}).model_dump()
    simulation, ok = await _timed(st, "tool", "simulate_payoff",
                                  asyncio.to_thread(formula.fn, st.profile, **{k: v for k, v in sim_args.items() if k != "order"}), strategy=code)
    if not ok:
        st.degraded, simulation = True, None
    else:
        st.compute["simulate_payoff"] = {"strategy": code, "months": chosen.months}       # the full schedule lives in the typed result
    st.pf = payoff_advisor.facts_for(summary, chosen)
    st.facts = dict(st.pf.values)
    st.pf_parts = (summary, chosen, verdict, simulation)
    t0 = time.perf_counter()
    _say(st, "advisor", "start", stream_mod.LABELS["answer"][0])
    ans = await payoff_advisor.answer(summary, chosen, st.pf, verdict)
    st.trace.append(TraceStep(kind="advise", name="advisor", ms=round((time.perf_counter() - t0) * 1000, 1),
                              detail={"source": ans.source, "retried": ans.diagnostics.retried, "first_try_ok": ans.diagnostics.first_try_ok,
                                      "reasons": ",".join(ans.diagnostics.final_reasons or ans.diagnostics.first_reasons)[:39]}))
    if ans.source != "model":
        st.degraded = True
    st.payoff = _payoff_analysis(st, summary, chosen, verdict, simulation, ans)


# ---- the buy event -------------------------------------------------------------------------------------------------------------

async def _pipeline_buy(st: _State) -> None:
    """Researcher (sanitized query only) -> buy_plans for the chosen car -> the advisor's answer. No eligible car: a clear error."""
    st.llm_trace = client.start_run()
    query = sanitize(st.inputs)                                    # the only thing the Researcher is told
    _say(st, "researcher", "start", stream_mod.LABELS["research"][0])
    t0 = time.perf_counter()
    found = await researcher.research(query)
    st.trace.append(TraceStep(kind="research", name="researcher", ms=round((time.perf_counter() - t0) * 1000, 1),
                              detail={"source": found.source, "retried": found.retried, "picks": len(found.picks), "reasons": ",".join(found.reasons)[:39]}))
    if found.source != "model":
        st.degraded = True
    choice = st.inputs.get("choice_id") or (found.picks[0].id if found.picks else None)
    if choice is None:
        raise ValueError("no car in the research pack fits that segment and budget")
    tool = session_tool(st.profile, "buy_plans")
    _say(st, "compute", "start", stream_mod.LABELS["buyplans"][0])
    plans, ok = await _timed(st, "tool", "buy_plans", tool.arun({"vehicle_id": choice}))
    if not ok:
        raise plans
    st.compute["buy_plans"] = to_plain(plans)
    pf = buy_advisor.facts_for(plans)
    st.facts = dict(pf.values)
    st.buy_parts = (plans, pf, found, query, choice)
    t0 = time.perf_counter()
    _say(st, "advisor", "start", stream_mod.LABELS["answer"][0])
    ans = await buy_advisor.answer(plans, pf)
    st.trace.append(TraceStep(kind="advise", name="advisor", ms=round((time.perf_counter() - t0) * 1000, 1),
                              detail={"source": ans.source, "retried": ans.diagnostics.retried, "first_try_ok": ans.diagnostics.first_try_ok,
                                      "reasons": ",".join(ans.diagnostics.final_reasons or ans.diagnostics.first_reasons)[:39]}))
    if ans.source != "model":
        st.degraded = True
    st.buy = BuyAnalysis(verdict=plans.verdict, query=query, picks=found.picks, research_source=found.source, choice_id=choice, plans=plans, answer=ans)  # type: ignore[arg-type]


# ---- the run -------------------------------------------------------------------------------------------------------------------

def _assemble(st: _State, data_version: str) -> AnalysisResult:
    return AnalysisResult(
        run_id=st.run_id, event=st.spec.name, feature=st.spec.feature, customer_id=st.profile.id, as_of=st.profile.as_of_date.isoformat(),
        prompt_version=PROMPT_VERSION, data_version=data_version, inputs=st.inputs, compute=st.compute, facts=st.facts or {},
        status=st.payoff.verdict if st.payoff else st.buy.verdict if st.buy else (st.facts or {}).get("overall_status"), summary=st.summary, advice=st.advice,
        payoff=st.payoff, buy=st.buy, trace=st.trace, llm_trace=st.llm_trace, degraded=st.degraded)


async def _pipeline(st: _State) -> None:
    if st.spec.pipeline == "payoff":
        return await _pipeline_payoff(st)
    if st.spec.pipeline == "buy":
        return await _pipeline_buy(st)
    st.llm_trace = client.start_run()
    await precompute(st)
    st.facts = build_facts(st)
    rules_task = asyncio.create_task(run_rules(st)) if st.spec.advise else None      # formulas only: overlaps the planner
    try:
        steps = await plan_steps(st)
        await execute(st, steps)
        writers = []                                       # the presenter (:1235) and the advisor (:1234) are different servers: in parallel
        if st.spec.explain:
            writers.append(explain(st))
        if rules_task is not None:
            await rules_task
            writers.append(advise(st))
        await asyncio.gather(*writers)
    finally:
        if rules_task is not None and not rules_task.done():
            rules_task.cancel()


async def _execute(st: _State, data_version: str) -> AnalysisResult:
    try:
        async with asyncio.timeout(RUN_TIMEOUT_S):
            await _pipeline(st)
    except TimeoutError:
        if st.spec.pipeline == "buy":
            if st.buy_parts is None:
                raise
            plans, pf, found, query, choice = st.buy_parts
            st.degraded = True
            st.trace.append(TraceStep(kind="fallback", name="timeout", ok=False, detail={"limit_s": RUN_TIMEOUT_S}))
            ans = buy_advisor._template_answer(plans, pf, "run_timeout", buy_advisor._Tally())
            st.buy = BuyAnalysis(verdict=plans.verdict, query=query, picks=found.picks, research_source=found.source, choice_id=choice, plans=plans, answer=ans)
            return _assemble(st, data_version)
        if st.spec.pipeline == "payoff":
            if st.pf_parts is None:             # no figures yet: nothing to show
                raise
            summary, chosen, verdict, simulation = st.pf_parts
            st.degraded = True
            st.trace.append(TraceStep(kind="fallback", name="timeout", ok=False, detail={"limit_s": RUN_TIMEOUT_S}))
            st.payoff = _payoff_analysis(st, summary, chosen, verdict, simulation, payoff_advisor.template_answer(verdict, st.pf, "run_timeout"))
            return _assemble(st, data_version)
        if st.facts is None:                    # not even the precompute finished: nothing to show
            raise
        st.degraded = True
        st.trace.append(TraceStep(kind="fallback", name="timeout", ok=False, detail={"limit_s": RUN_TIMEOUT_S}))
        if st.spec.explain and st.summary is None:
            st.summary = _template_summary(st)
        if st.spec.advise and st.advice is None:
            if st.rules is None:                            # the rules did not finish either: compute them now (formulas only, no model)
                await run_rules(st)
            st.advice = advisor.template_advice(st.rules, reason="run_timeout")
    return _assemble(st, data_version)


def is_transient(result: AnalysisResult) -> bool:
    """Advice or an answer served from the template because the advisor ran out of time, was unreachable, or the run hit its limit: temporary."""
    for part in (result.advice, result.payoff.answer if result.payoff else None):
        if part and part.source == "template" and TRANSIENT_REASONS & set(part.diagnostics.first_reasons + part.diagnostics.final_reasons):
            return True
    return False


async def _rerun(event: str, profile: Profile, params: dict) -> None:
    await asyncio.sleep(RERUN_DELAY_S)
    try:
        await run(event, profile, params, rerun=True)
    except Exception as e:                      # superseded by a newer run, or failed again: the page keeps what it has
        log.info("background rerun ended: %s", type(e).__name__)


def _schedule_rerun(event: str, profile: Profile, params: dict) -> None:
    task = asyncio.ensure_future(_rerun(event, profile, params))
    _reruns.add(task)
    task.add_done_callback(_reruns.discard)


async def run(event: str, profile: Profile, params: dict, refresh: bool = False, rerun: bool = False) -> AnalysisResult:
    """KeyError for an unknown event, pydantic.ValidationError for bad params. `profile` is the signed-in customer's own.
    rerun: the background retry of a transient result: skips the cache read, never restarts a run in flight, never schedules another."""
    spec = get_event(event)
    args = spec.params.model_validate(params).model_dump(mode="json")
    data_version = loader.cache_key()[:12]
    key = cache.cache_key(profile.id, event, args, PROMPT_VERSION, data_version)
    if spec.cache != "none" and not refresh and not rerun and (hit := cache.get(key)) is not None:
        return hit.with_trace(TraceStep(kind="cache_hit", name=event))

    async def go() -> AnalysisResult:
        rs = _NEW_STREAM.get() or stream_mod.RunStream(uuid.uuid4().hex[:12])
        _NEW_STREAM.set(None)                   # a background rerun started from here must not inherit this run's stream
        rs.started = True
        stream_mod.register(key, rs)
        rs.progress("orchestrator", "start", stream_mod.LABELS["start"])
        try:
            result = await _execute(_State(spec, profile, args, run_id=rs.run_id, stream=rs), data_version)
            if spec.cache != "none":
                transient = is_transient(result)
                cache.put(key, result, ttl=TRANSIENT_TTL_S if transient else None)
                if transient and not rerun:
                    _schedule_rerun(event, profile, args)
            rs.finish(result)
            return result
        except asyncio.CancelledError:
            rs.fail(stream_mod.LABELS["superseded"])
            raise
        except BaseException:
            rs.fail(stream_mod.LABELS["failed"])
            raise
        finally:
            stream_mod.release(key, rs)

    return await cache.run_exclusive(profile.id, event, key, go, restart=refresh)


_drivers: set[asyncio.Task] = set()
_NEW_STREAM: contextvars.ContextVar[stream_mod.RunStream | None] = contextvars.ContextVar("new_stream", default=None)


async def open_stream(event: str, profile: Profile, params: dict, refresh: bool = False):
    """Validates like run() (KeyError / ValidationError raise here, before any event), then returns an async iterator of StreamEvents:
    a cached result replays its stored trace (cached=True); a run in flight is joined; otherwise a new run is started. Ends after the
    final "result" (or "error") event. A subscriber that goes away leaves the run going."""
    spec = get_event(event)
    args = spec.params.model_validate(params).model_dump(mode="json")
    key = cache.cache_key(profile.id, event, args, PROMPT_VERSION, loader.cache_key()[:12])
    hit = None if (spec.cache == "none" or refresh) else cache.get(key)
    events: list | None = None
    sub: stream_mod.RunStream | None = None             # the stream this caller listens to
    if hit is not None:
        events = stream_mod.replay(hit.with_trace(TraceStep(kind="cache_hit", name=event)))
    else:
        sub = None if refresh else stream_mod.active(key)
        if sub is None:                                 # nothing in flight: start the run, with a stream made here
            sub = stream_mod.RunStream(uuid.uuid4().hex[:12])
            _NEW_STREAM.set(sub)
            _drivers.add(driver := asyncio.ensure_future(_drive(sub, event, profile, params, refresh)))   # copies the context: go() finds it
            driver.add_done_callback(_drivers.discard)
            _NEW_STREAM.set(None)
    q = sub.subscribe() if sub is not None else None

    async def gen():
        try:
            if events is not None:
                for ev in events:
                    yield ev
                return
            while (ev := await q.get()) is not None:
                yield ev
        finally:
            if sub is not None and q is not None:
                sub.unsubscribe(q)               # the run itself carries on: its result still lands in the cache
    return gen()


async def _drive(rs: stream_mod.RunStream, event: str, profile: Profile, params: dict, refresh: bool) -> None:
    """Runs the event for a stream subscriber. If run() did not use `rs` (the work was already done or in flight by the time it started),
    the subscriber still gets the result."""
    try:
        result = await run(event, profile, params, refresh=refresh)
    except asyncio.CancelledError:
        raise
    except BaseException as e:
        if not rs.started:
            rs.fail(stream_mod.LABELS["superseded"] if isinstance(e, cache.RunSuperseded) else stream_mod.LABELS["failed"])
        return
    if not rs.started:
        rs.progress("orchestrator", "start", stream_mod.LABELS["start"])
        for step in result.trace:
            rs.step(step)
        rs.finish(result)


def is_cache_hit(result: AnalysisResult) -> bool:
    return bool(result.trace) and result.trace[-1].kind == "cache_hit"


def _enabled(name: str) -> bool:
    return os.environ.get(name, "true").strip().lower() in ("1", "true", "yes")


async def warm_featured(profiles: dict[str, Profile], ids: list[str]) -> None:
    """At startup: run dashboard_open for the demo personas, one after the other (the models are single-user servers), so their first
    page load is a cache hit. Runs in the background; never raises."""
    if not _enabled("AGENT_WARM_ON_STARTUP"):
        return
    for cid in ids:
        if cid in profiles:
            await _warm(profiles[cid])


async def warm(profile: Profile) -> None:
    """Start dashboard_open at sign-in so the result is usually cached by the time the page asks. Never raises."""
    if _enabled("AGENT_WARM_ON_LOGIN"):
        await _warm(profile)


async def _warm(profile: Profile) -> None:
    try:
        await run("dashboard_open", profile, {})
    except Exception as e:
        log.warning("warm-up failed: %s", type(e).__name__)
