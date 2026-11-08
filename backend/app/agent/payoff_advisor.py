"""The simulate_payoff answer: "should I pay it off?"

Code decides everything that is a judgment: the verdict and the recommended strategy (from payoff_priority, through
payoff_strategies_summary's `recommended`), and every figure (the strategy's months, interest saved, months the p10 cash band is under
the cushion, and the tradeoff_low_cushion / lump_exceeds_debt / exceeds_safe_lump warnings). The model writes at most two plain
sentences about the chosen strategy using {placeholders} for those figures. It does not say yes or no and does not judge.

Checks: the sentences go through the same grounding checks as the dashboard (figures against the strategy's own facts, no judgments,
plain English), plus: at most two sentences, at least one figure, and every warning that applies must be mentioned (one of its
figures appears). One retry at temperature 0.4, then the deterministic template. Past the advisor budget, or with the model down,
the template is served. With no debt there is no model call at all.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel, Field

from ..formulas.payoff import PayoffStrategiesSummary, StrategySummary
from ..llm import client, config as llm_config, grounding
from .advisor import ADVISOR_MAX_TOKENS, months as fmt_months, pct, tidy, usd
from .models import AdviceDiagnostics, BadReply, GroundingSummary, PayoffAnswer, PayoffVerdict

MAX_SENTENCES = 2

# plain phrases, fixed in code
STRATEGY_TITLE = {
    "A": "Extra money to the highest-rate debt first", "B": "Extra money to the smallest balance first",
    "C": "A lump sum now, then minimum payments", "D": "A lump sum now, then extra money to the highest-rate debt",
    "E": "Minimum payments only", "F": "Your custom order"}


class AnswerModel(BaseModel):
    answer: str = Field(description="At most two short sentences of plain English, using the placeholders for every figure")


RESPONSE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}}

PAYOFF_PROMPT = (
    "You are a careful financial coach. The customer asks whether to pay off their debt. The app has already decided the answer and "
    "chosen the plan. You explain what the plan does, in at most two short plain sentences.\n"
    "Rules:\n"
    "- Use the {placeholders} listed for every amount and length of time, written in braces exactly; the app fills in the real figures. "
    "Never type a number yourself, and use only the listed placeholders.\n"
    "- Mention each point listed under \"Be sure to mention\", using its placeholders.\n"
    "- Do not say yes or no, do not recommend, and do not state a judgment, risk level or reassurance. The app shows its answer separately.\n"
    "- Plain English only. No codes or field names.\n"
    'Reply with only JSON: {"answer":"<one or two sentences>"}')


# ---- facts ---------------------------------------------------------------------------------------------------------------------

@dataclass
class PayoffFacts:
    values: dict[str, float]                  # placeholder name -> raw figure (what the grounding check compares against)
    text: dict[str, str]                      # placeholder name -> what the customer reads
    means: dict[str, str]
    mentions: dict[str, tuple[str, ...]]      # a warning that applies -> the placeholders, one of which must appear
    code: str
    months: int | None


def _warning(chosen: StrategySummary, code: str):
    return next((w for w in chosen.warnings if w.code == code), None)


def verdict_for(summary: PayoffStrategiesSummary, chosen: StrategySummary | None) -> PayoffVerdict:
    """Decided in code from payoff_priority's recommendation and the chosen strategy's warnings."""
    rec = summary.recommended
    if summary.no_debt or rec is None or rec.code is None:
        return "no_debt"
    if rec.reason_code == "keep_cash_earns_more":
        return "keep_cash"
    if rec.code == "E":
        return "no_room"                       # nothing to pay with: no surplus and no lump sum above the cushion floor
    if chosen and any(w.code in ("tradeoff_low_cushion", "exceeds_safe_lump") for w in chosen.warnings):
        return "pay_with_tradeoff"
    return "pay_it_off"


def facts_for(summary: PayoffStrategiesSummary, chosen: StrategySummary) -> PayoffFacts:
    """The figures the sentences may use. A zero is never offered ("saving $0", "0 months" reads as broken)."""
    values: dict[str, float] = {}
    text: dict[str, str] = {}
    means: dict[str, str] = {}
    mentions: dict[str, tuple[str, ...]] = {}

    def add(name: str, raw: float | None, fmt: Callable, meaning: str) -> bool:
        if raw is None or (isinstance(raw, (int, float)) and abs(raw) < 0.5):
            return False
        values[name], text[name], means[name] = float(raw), fmt(raw), meaning
        return True

    add("payoff_months", chosen.months, fmt_months, "how long until the debt is gone, with the unit")
    add("interest_saved", chosen.interest_saved, usd, "interest saved compared with paying only the minimums")
    if (w := _warning(chosen, "tradeoff_low_cushion")) is not None:
        names = [n for n, ok in (
            ("cushion_low_point", add("cushion_low_point", w.amount, usd, "the lowest cash balance in a bad stretch, under this plan")),
            ("cushion_target", add("cushion_target", summary.cushion, usd, "the size of the safety cushion")),
            ("cushion_dip_months", add("cushion_dip_months", chosen.months_p10_below_cushion, fmt_months,
                                       "how many month-ends cash is under the cushion in a bad stretch, with the unit"))) if ok]
        mentions["tradeoff_low_cushion"] = tuple(names)
    if (w := _warning(chosen, "exceeds_safe_lump")) is not None:
        mentions["exceeds_safe_lump"] = ("lump_over_safe",) if add(
            "lump_over_safe", w.amount, usd, "how much the lump sum is above the safe amount") else ()
    if (w := _warning(chosen, "lump_exceeds_debt")) is not None:
        names = [n for n, ok in (
            ("unused_lump", add("unused_lump", w.unused_amount, usd, "the part of the lump sum the debts cannot take; it stays in cash")),
            ("safe_lump", add("safe_lump", summary.safe_lump_sum, usd, "the safe lump sum"))) if ok]
        mentions["lump_exceeds_debt"] = tuple(n for n in names if n == "unused_lump")
    if verdict_for(summary, chosen) == "keep_cash" and summary.highest_debt_apr is not None and summary.savings_rate is not None:
        for name, raw, meaning in (("debt_apr", summary.highest_debt_apr, "the interest rate on the dearest debt"),
                                   ("savings_rate", summary.savings_rate, "what savings earn")):
            values[name], text[name], means[name] = raw, pct(raw), meaning          # rates are fractions: not subject to the "zero" rule
        return PayoffFacts(values, text, means, {"keep_cash": ("debt_apr", "savings_rate")}, chosen.code, chosen.months)
    result = tuple(n for n in ("payoff_months", "interest_saved") if n in values)          # the plan's main result is always worth a line
    if result:
        mentions = {"result": result, **mentions}
    return PayoffFacts(values, text, means, {k: v for k, v in mentions.items() if v}, chosen.code, chosen.months)


# ---- the template --------------------------------------------------------------------------------------------------------------

def template_text(verdict: PayoffVerdict, pf: PayoffFacts | None) -> str:
    """Deterministic, built from the figures alone; it passes the same checks."""
    return tidy(grounding.fill(template_source(verdict, pf), pf.text if pf else {}))


def template_source(verdict: PayoffVerdict, pf: PayoffFacts | None) -> str:
    """The template with its {placeholders} still in it (the model's example sentence)."""
    if verdict == "no_debt":
        return "You have no debt to pay off."
    if verdict == "keep_cash":
        if pf is not None and "keep_cash" in pf.mentions:
            return "Your debt costs {debt_apr} a year, less than the {savings_rate} your savings earn, so extra payments are optional."
        return "Your debt costs less than your savings earn, so extra payments are optional."
    if verdict == "no_room":
        return "There is no money left over each month or above your cushion to put toward the debt, so only the minimum payments continue."
    assert pf is not None
    if "payoff_months" in pf.values and "interest_saved" in pf.values:
        first = "You would be debt-free in {payoff_months}, saving about {interest_saved} in interest."
    elif "interest_saved" in pf.values:
        first = "The lump sum clears the debt right away, saving about {interest_saved} in interest."
    elif "payoff_months" in pf.values:
        first = "You would be debt-free in {payoff_months}."
    else:
        first = "At these payments the debt is not paid off within the simulation limit." if pf.months is None else "The plan clears the debt right away."
    second = ""
    if "tradeoff_low_cushion" in pf.mentions:
        parts = [p for p in ("cushion_low_point", "cushion_target", "cushion_dip_months") if p in pf.values]
        second = {("cushion_low_point", "cushion_target", "cushion_dip_months"):
                  "In a bad stretch your cash would dip to about {cushion_low_point}, under your {cushion_target} cushion, for {cushion_dip_months}.",
                  ("cushion_low_point", "cushion_target"):
                  "In a bad stretch your cash would dip to about {cushion_low_point}, under your {cushion_target} cushion.",
                  ("cushion_low_point", "cushion_dip_months"): "In a bad stretch your cash would dip to about {cushion_low_point}, for {cushion_dip_months}.",
                  ("cushion_low_point",): "In a bad stretch your cash would dip to about {cushion_low_point}.",
                  }.get(tuple(parts), "In a bad stretch your cash would dip under your cushion.")
    elif "exceeds_safe_lump" in pf.mentions:
        second = "The lump sum is {lump_over_safe} more than the amount that keeps your cushion intact."
    elif "lump_exceeds_debt" in pf.mentions:
        second = "Your debts take only part of the lump sum, so {unused_lump} stays in cash."
    return f"{first} {second}".strip()


# ---- checking ------------------------------------------------------------------------------------------------------------------

def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def check_answer(raw: str, pf: PayoffFacts, verdict: PayoffVerdict) -> tuple[str, list[str], grounding.GroundingReport]:
    """(the filled text, reason codes of the failures, the grounding report). Codes only."""
    filled = tidy(grounding.fill(raw, pf.text) or "")
    report = grounding.check(filled, pf.values, judgments=True, style=True, hints=False)
    reasons = ["figure_not_in_facts"] if report.unmatched else []
    reasons += [c.field if c.field in ("judgment", "style") else f"contradicts_{c.field}" for c in report.conflicts]
    used = {n for n in pf.text if "{" + n + "}" in raw}
    if pf.text and not used:
        reasons.append("no_placeholder")
    if any(not (set(names) & used) for names in pf.mentions.values()):
        reasons.append("warning_not_mentioned")
    if re.search(r"\$0(?!\d|,\d|\.\d)", filled):
        reasons.append("zero_figure")
    if _sentences(filled) > MAX_SENTENCES:
        reasons.append("too_long")
    if not filled.strip():
        reasons.append("empty")
    return filled, list(dict.fromkeys(reasons)), report


def model_view(summary: PayoffStrategiesSummary, chosen: StrategySummary, pf: PayoffFacts) -> str:
    lines = [f"Question: should I pay off my debt? (The app answers that separately; you do not.)",
             f"The plan: {STRATEGY_TITLE[chosen.code]}.",
             "Placeholders you may use (write them exactly, with the braces):"]
    lines += [f"{{{k}}} = {v}" for k, v in pf.means.items()]
    notes = {"result": "what the plan achieves", "tradeoff_low_cushion": "your cash would run under the safety cushion for a while in a bad stretch",
             "exceeds_safe_lump": "the lump sum is more than the amount that keeps the cushion intact",
             "keep_cash": "the debt's interest rate is lower than what savings earn (use both rates)",
             "lump_exceeds_debt": "the debts take only part of the lump sum, and the rest stays in cash"}
    if pf.mentions:
        lines.append("Be sure to mention: " + "; ".join(f"{notes[k]} (use {', '.join('{' + n + '}' for n in names)})" for k, names in pf.mentions.items()) + ".")
    lines.append("Example (adapt it, keeping the braces): " + template_source(verdict_for(summary, chosen), pf))
    return "\n".join(lines)


# ---- the run -------------------------------------------------------------------------------------------------------------------

@dataclass
class _Tally:
    retried: bool = False
    first_ok: bool = False
    first_reasons: list[str] = field(default_factory=list)
    final_reasons: list[str] = field(default_factory=list)
    calls: int = 0
    checked: int = 0
    bad: list[BadReply] = field(default_factory=list)


def _answer(text: str, source: str, t: _Tally) -> PayoffAnswer:
    return PayoffAnswer(text=text, source=source, grounding=GroundingSummary(ok=True, checked=t.checked, retried=t.retried),
                        diagnostics=AdviceDiagnostics(first_try_ok=t.first_ok, retried=t.retried, model_calls=t.calls,
                                                      template_moves=int(source == "template"), first_reasons=t.first_reasons,
                                                      final_reasons=t.final_reasons, bad_replies=t.bad))


def template_answer(verdict: PayoffVerdict, pf: PayoffFacts | None, reason: str, tally: _Tally | None = None) -> PayoffAnswer:
    t = tally or _Tally()
    t.first_reasons, t.final_reasons = t.first_reasons or [reason], t.final_reasons or [reason]
    return _answer(template_text(verdict, pf), "template", t)


def no_debt_answer() -> PayoffAnswer:
    t = _Tally(first_ok=True)
    return template_answer("no_debt", None, "no_debt", t)


async def answer(summary: PayoffStrategiesSummary, chosen: StrategySummary, pf: PayoffFacts, verdict: PayoffVerdict,
                 budget_s: float | None = None) -> PayoffAnswer:
    """The model's two sentences, checked; the template when they fail, when the model is down, or past the advisor budget."""
    limit = llm_config.advisor_budget_s() if budget_s is None else budget_s
    tally = _Tally()
    try:
        async with asyncio.timeout(None if limit == math.inf else limit):
            return await _answer_with_model(summary, chosen, pf, verdict, tally)
    except TimeoutError:
        return template_answer(verdict, pf, "budget_exceeded", _Tally(retried=tally.retried, calls=tally.calls))


async def _answer_with_model(summary, chosen, pf: PayoffFacts, verdict, tally: _Tally) -> PayoffAnswer:
    messages = [{"role": "system", "content": PAYOFF_PROMPT}, {"role": "user", "content": model_view(summary, chosen, pf)}]
    ask = lambda msgs, **kw: client.complete("advisor", msgs, json_schema=AnswerModel, max_tokens=ADVISOR_MAX_TOKENS, schema_in_prompt=False,
                                             response_schema=RESPONSE_SCHEMA, **kw)

    def note(e: client.LLMResponseError):
        tally.bad += [BadReply(finish_reason=a["finish_reason"], completion_tokens=a["completion_tokens"], max_tokens=e.max_tokens) for a in e.attempts]

    try:
        first = await ask(messages)
    except client.LLMUnavailable:
        return template_answer(verdict, pf, "llm_unavailable", tally)
    except client.LLMError as e:
        if isinstance(e, client.LLMResponseError):
            note(e)
        return template_answer(verdict, pf, "llm_bad_reply", tally)
    tally.calls = 1
    text, reasons, report = check_answer(first.parsed.answer, pf, verdict)
    tally.first_ok, tally.first_reasons = not reasons, reasons
    if reasons:
        tally.retried = True
        complaint = ("Problems: " + ", ".join(reasons).replace("_", " ") + ". Write at most two plain sentences, adapting the plan above, "
                     "using the listed {placeholders} (and each point under \"Be sure to mention\"), with no judgment. Reply with only the JSON.")
        try:
            retry = await ask(messages + [{"role": "assistant", "content": first.content}, {"role": "user", "content": complaint}],
                              temperature=grounding.RETRY_TEMPERATURE)
            tally.calls = 2
            text, reasons, report = check_answer(retry.parsed.answer, pf, verdict)
            tally.final_reasons = reasons
        except client.LLMError as e:
            if isinstance(e, client.LLMResponseError):
                note(e)
            tally.final_reasons = ["llm_unavailable" if isinstance(e, client.LLMUnavailable) else "llm_bad_reply"]
            reasons = tally.final_reasons
    if reasons:
        return _answer(template_text(verdict, pf), "template", tally)
    tally.checked = len(report.numbers)
    return _answer(text, "model", tally)
