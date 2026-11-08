"""Orchestrator data shapes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_STEPS = 4
REASON_MAX = 80


class PlanStep(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field("", description="A short phrase: why this calculation could change what the customer should do")

    @field_validator("reason", mode="before")
    @classmethod
    def _short(cls, v: Any) -> Any:
        return v[:REASON_MAX] if isinstance(v, str) else v


class Plan(BaseModel):
    """What the planner returns: at most MAX_STEPS steps (enforced in code, so a long plan loses its extra steps instead of failing)."""
    steps: list[PlanStep] = Field(default_factory=list)


TraceKind = Literal["precompute", "plan", "plan_rejected", "tool", "rules", "explain", "advise", "fallback", "cache_hit"]


class TraceStep(BaseModel):
    """One step of a run. `detail` holds flags, codes and counts only: never prompt text, model text or rejected arguments."""
    model_config = ConfigDict(extra="forbid")
    kind: TraceKind
    name: str
    ms: float = 0.0
    ok: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)


class GroundingSummary(BaseModel):
    ok: bool
    checked: int
    retried: bool


class HeadlineSummary(BaseModel):
    text: str
    source: Literal["model", "template"]
    grounding: GroundingSummary


class AdviceMove(BaseModel):
    rank: int
    move_code: str                           # always one of the candidate moves (checked in code)
    title_text: str                          # placeholders already filled in
    why_text: str
    source: Literal["model", "template"]
    priority: int                            # the waterfall step the move serves (0 = close the monthly gap, 8 = an opportunity)
    addresses_current_step: bool


class BadReply(BaseModel):
    """One model reply that was not usable JSON: how it ended, never what it said. finish_reason "length" means it was cut off."""
    finish_reason: str
    completion_tokens: int
    max_tokens: int | None


class AdviceDiagnostics(BaseModel):
    """How the advisor's answer fared, as counts and reason codes only (never model text)."""
    first_try_ok: bool                       # the model's first answer passed every check
    retried: bool
    model_calls: int
    template_moves: int                      # moves served from the template
    first_reasons: list[str] = Field(default_factory=list)     # why the first answer failed: unknown_move_code, figure_not_in_facts,
    bad_replies: list[BadReply] = Field(default_factory=list)  # one per unusable reply (llm_bad_reply)
    final_reasons: list[str] = Field(default_factory=list)     # judgment, style, contradicts_<label>, no_placeholder, zero_figure, ...


class Advice(BaseModel):
    """What the customer is told to do next. The verdict and the candidates come from code; the model only picks and words them."""
    verdict: Literal["on_track", "plan_ahead", "act_now"]      # set by code (rules.verdict)
    current_step: int | Literal["gap"] | None
    moves: list[AdviceMove]                  # at most 3, ranked
    notes: list[str] = Field(default_factory=list)             # e.g. "Retirement saving comes before education saving."
    source: Literal["model", "template", "mixed"]
    grounding: GroundingSummary
    diagnostics: AdviceDiagnostics


class AnalysisResult(BaseModel):
    run_id: str
    event: str
    feature: str
    customer_id: str
    as_of: str
    prompt_version: str
    data_version: str
    inputs: dict[str, Any]
    compute: dict[str, Any]                  # formula results by name (plain JSON)
    facts: dict[str, Any]                    # the fact sheet the explanation used
    status: str | None                       # overall_status, decided in code
    summary: HeadlineSummary | None
    research: list[Any] = Field(default_factory=list)       # empty in v1
    advice: Advice | None = None
    trace: list[TraceStep]
    llm_trace: list[dict[str, Any]]          # the LLM client's per-run trace
    degraded: bool = False                   # a fallback was used somewhere

    def with_trace(self, step: TraceStep) -> "AnalysisResult":
        return self.model_copy(update={"trace": [*self.trace, step]})
