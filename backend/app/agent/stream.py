"""Progress events for an agent run (the /stream endpoint).

Every run records its events in a RunStream; the run's trace points feed it. Each subscriber has its own asyncio.Queue, pre-filled with
what already happened, so a late joiner sees the whole run. Labels come from the fixed catalog below: plain language, written in code.

Never in an event: prompts, model text, reasoning, customer data or amounts. The shape enforces it: `detail` is a model with exactly five
optional fields (extra keys are rejected), labels are catalog constants, and the only free text is a formula name from the registry.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .models import AnalysisResult, TraceStep

Agent = Literal["orchestrator", "compute", "advisor", "presenter", "researcher"]
Phase = Literal["start", "step", "done", "error"]


class StreamDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formula: str | None = None
    ms: float | None = None
    check: str | None = None
    outcome: Literal["passed", "retried", "template"] | None = None
    cached: bool | None = None


class StreamEvent(BaseModel):
    """One server-sent event. type "progress": agent, phase, label and detail are set. type "result": `result` is the full AnalysisResult.
    type "error": the run produced no result (label says so)."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["progress", "result", "error"] = "progress"
    run_id: str
    seq: int
    t_ms: int
    agent: Agent | None = None
    phase: Phase | None = None
    label: str | None = None
    detail: StreamDetail = StreamDetail()
    result: AnalysisResult | None = None


# ---- the label catalog -------------------------------------------------------------------------------------------------------------

LABELS = {
    "start": "Starting the check-up",
    "ready": "Check-up ready",
    "calc": ("Running the calculations", "Calculations ready"),
    "plan": ("Planning which calculations to run", "Calculations chosen"),
    "plan_default": "Using the standard set of calculations",
    "rules": "Applying the decision rules",
    "summary": ("Writing the summary", "Summary written"),
    "advice": ("Choosing your next moves", "Next moves ready"),
    "answer": ("Writing the answer", "Answer ready"),
    "check": "Checking the answer against the numbers",
    "retry": "Retrying with stricter wording",
    "standard": "Using the standard wording",
    "compare": ("Comparing 5 payoff strategies", "Strategies compared"),
    "simulate": ("Simulating the chosen plan", "Plan simulated"),
    "slow": "This is taking longer than expected; showing what is ready",
    "failed": "The check-up could not be completed",
    "superseded": "A newer check-up replaced this one",
    "other_calc": "Running a calculation",
}
FORMULA_LABELS = {
    "forecast_liquid_summary": "Forecasting your cash",
    "paycheck_to_paycheck": "Checking how much room your paychecks leave",
    "job_loss_stress": "Testing what a job loss would do",
    "payoff_strategies_summary": LABELS["compare"][1],
    "simulate_payoff": LABELS["simulate"][1],
}
LABEL_TEXTS = {t for v in LABELS.values() for t in ((v,) if isinstance(v, str) else v)} | set(FORMULA_LABELS.values())


def _calc_label(name: str) -> str:
    return FORMULA_LABELS.get(name, LABELS["other_calc"])


# One trace step becomes zero or more (agent, phase, label, detail) tuples. Stored traces replay through the same function.
def progress_for(step: TraceStep) -> list[tuple[Agent, Phase, str, dict[str, Any]]]:
    kind, d = step.kind, step.detail
    ms = {"ms": step.ms}
    if kind in ("precompute", "tool"):
        return [("compute", "step" if step.ok else "error", _calc_label(step.name), {"formula": step.name, **ms})]
    if kind == "rules":
        return [("compute", "step" if step.ok else "error", LABELS["rules"], {"formula": step.name, **ms})]
    if kind == "plan":
        return [("orchestrator", "done", LABELS["plan"][1], ms)]
    if kind == "fallback" and step.name == "plan":
        return [("orchestrator", "done", LABELS["plan_default"], {"outcome": "template", **ms})]
    if kind == "explain":
        out: list[tuple[Agent, Phase, str, dict[str, Any]]] = []
        if d.get("retried"):
            out.append(("presenter", "step", LABELS["retry"], {}))
        if d.get("template"):
            out.append(("presenter", "step", LABELS["standard"], {"outcome": "template"}))
        outcome = "template" if d.get("template") else "retried" if d.get("retried") else "passed"
        return out + [("presenter", "done", LABELS["summary"][1], {"check": "grounding", "outcome": outcome, **ms})]
    if kind == "advise":
        out = []
        if d.get("retried"):
            out.append(("advisor", "step", LABELS["retry"], {}))
        template = d.get("source") == "template"
        out.append(("advisor", "step", LABELS["standard"] if template else LABELS["check"],
                    {"check": "grounding", "outcome": "template" if template else "retried" if d.get("retried") else "passed"}))
        outcome = "template" if template else "retried" if d.get("retried") else "passed"
        done = LABELS["answer"][1] if "moves" not in d else LABELS["advice"][1]
        return out + [("advisor", "done", done, {"outcome": outcome, **ms})]
    if kind == "fallback" and step.name in ("explain", "advise"):
        agent: Agent = "presenter" if step.name == "explain" else "advisor"
        return [(agent, "done", LABELS["standard"], {"outcome": "template", **ms})]
    if kind == "fallback" and step.name == "timeout":
        return [("orchestrator", "step", LABELS["slow"], {})]
    return []                                                       # plan_rejected, cache_hit: nothing to say


class RunStream:
    """The events of one run. emit() appends and fans out to every subscriber's queue; subscribe() starts with what happened so far."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.t0 = time.perf_counter()
        self.events: list[StreamEvent] = []
        self._subs: set[asyncio.Queue] = set()
        self.closed = False
        self.started = False                      # the run itself reports into this stream (not a cache hit or someone else's run)

    def _now(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)

    def _push(self, ev: StreamEvent) -> None:
        self.events.append(ev)
        for q in self._subs:
            q.put_nowait(ev)

    def progress(self, agent: Agent, phase: Phase, label: str, **detail: Any) -> None:
        if self.closed:
            return
        self._push(StreamEvent(run_id=self.run_id, seq=len(self.events), t_ms=self._now(), agent=agent, phase=phase, label=label,
                               detail=StreamDetail(**detail)))

    def step(self, step: TraceStep) -> None:
        for agent, phase, label, detail in progress_for(step):
            self.progress(agent, phase, label, **detail)

    def finish(self, result: AnalysisResult) -> None:
        if self.closed:
            return
        self.progress("orchestrator", "done", LABELS["ready"])
        self._push(StreamEvent(type="result", run_id=self.run_id, seq=len(self.events), t_ms=self._now(), result=result))
        self._close()

    def fail(self, label: str) -> None:
        """The run produced no result: an error progress event, then a final error event."""
        if self.closed:
            return
        self.progress("orchestrator", "error", label)
        self._push(StreamEvent(type="error", run_id=self.run_id, seq=len(self.events), t_ms=self._now(), label=label))
        self._close()

    def _close(self) -> None:
        self.closed = True
        for q in self._subs:
            q.put_nowait(None)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for ev in self.events:
            q.put_nowait(ev)
        if self.closed:
            q.put_nowait(None)
        else:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


class TraceList(list):
    """st.trace: every step appended is also reported to the stream."""

    def __init__(self, stream: RunStream):
        super().__init__()
        self._stream = stream

    def append(self, step: TraceStep) -> None:
        super().append(step)
        self._stream.step(step)

    def extend(self, steps) -> None:
        for s in steps:
            self.append(s)

    def __iadd__(self, steps):
        self.extend(steps)
        return self


def replay(result: AnalysisResult) -> list[StreamEvent]:
    """A finished run's events, rebuilt from its stored trace, all marked cached. Instant: t_ms is 0."""
    rs = RunStream(result.run_id)
    rs.progress("orchestrator", "start", LABELS["start"], cached=True)
    for step in result.trace:
        for agent, phase, label, detail in progress_for(step):
            detail = {k: v for k, v in detail.items() if k != "ms"}
            rs.progress(agent, phase, label, cached=True, **detail)
    rs.progress("orchestrator", "done", LABELS["ready"], cached=True)
    rs._push(StreamEvent(type="result", run_id=result.run_id, seq=len(rs.events), t_ms=0, result=result))
    for e in rs.events:
        e.t_ms = 0
    return rs.events


# ---- in-flight runs, by cache key ----------------------------------------------------------------------------------------------------

_ACTIVE: dict[tuple, RunStream] = {}


def active(key: tuple) -> RunStream | None:
    rs = _ACTIVE.get(key)
    return rs if rs is not None and not rs.closed else None


def register(key: tuple, rs: RunStream) -> None:
    _ACTIVE[key] = rs


def release(key: tuple, rs: RunStream) -> None:
    if _ACTIVE.get(key) is rs:
        del _ACTIVE[key]


def clear() -> None:
    _ACTIVE.clear()
