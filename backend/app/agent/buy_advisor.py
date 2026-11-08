"""The simulate_buy answer: at most two plain sentences about the recommended way to pay, in the payoff answer's pattern.

Code decides the verdict and the plan (formulas.buy) and every figure. The model words at most two sentences with {placeholders};
the same grounding checks apply (figures must match the plan's facts, no judgments, plain English, every warning mentioned), one
retry, then the deterministic template. The APR comes from the plan result (set in code); the Researcher never saw it."""
from __future__ import annotations

import asyncio
import math

from pydantic import BaseModel, Field

from ..formulas.buy import BuyPlans
from ..llm import client, config as llm_config, grounding
from .advisor import ADVISOR_MAX_TOKENS, pct, tidy, usd
from .models import PayoffAnswer
from .payoff_advisor import PayoffFacts, _Tally, _answer, check_answer


class AnswerModel(BaseModel):
    answer: str = Field(description="At most two short sentences of plain English, using the placeholders for every figure")


RESPONSE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}}

PROMPT = (
    "You are a careful financial coach. The customer asks how to pay for a car. The app has already chosen the way to pay and decided the "
    "verdict. You explain what that way to pay means, in at most two short plain sentences.\n"
    "Rules:\n"
    "- Use the {placeholders} listed for every amount, rate and share, written in braces exactly; the app fills in the real figures. "
    "Never type a number yourself, and use only the listed placeholders.\n"
    "- Mention each point listed under \"Be sure to mention\", using its placeholders.\n"
    "- Do not say yes or no, do not recommend, and do not state a judgment, risk level or reassurance. The app shows its answer separately.\n"
    "- Plain English only. No codes or field names.\n"
    'Reply with only JSON: {"answer":"<one or two sentences>"}')


def facts_for(p: BuyPlans) -> PayoffFacts:
    chosen = p.plans[p.recommended]
    values: dict[str, float] = {}
    text: dict[str, str] = {"car": p.vehicle_name}
    means: dict[str, str] = {"car": "the car's make and model"}
    mentions: dict[str, tuple[str, ...]] = {}

    def add(name: str, raw: float, fmt, meaning: str) -> bool:
        if abs(raw) < 0.5 and not name.endswith(("_share", "_rate", "_apr")):
            return False
        values[name], text[name], means[name] = float(raw), fmt(raw), meaning
        return True

    add("price", p.price, usd, "the car's starting price")
    if chosen.loan_amount > 0:
        add("payment", chosen.monthly_payment, usd, "the monthly payment")
        add("total_interest", chosen.total_interest, usd, "the interest paid over the whole loan")
        add("loan_apr", p.loan_apr, pct, "the loan's yearly interest rate")
    if chosen.down_payment > 0 and chosen.loan_amount > 0:
        add("down_payment", chosen.down_payment, usd, "the cash put down")
    if p.recommended == "cash":
        add("interest_avoided", p.plans["finance"].total_interest, usd, "the interest a loan would have cost")
    if not chosen.payment_ok and chosen.loan_amount > 0:
        add("payment_share", chosen.payment_share_of_gross, pct, "the payment as a share of gross monthly income")
        add("payment_limit_share", 0.10, pct, "the share of gross income a car payment should stay under")
        mentions["payment_over"] = ("payment_share",)
    if not chosen.cushion_holds:
        add("cushion_shortfall", chosen.cushion_shortfall, usd, "how far cash would fall under the cushion target in a bad stretch")
        mentions["cushion_short"] = ("cushion_shortfall",)
    return PayoffFacts(values, text, means, mentions, p.recommended, None)


def template_source(p: BuyPlans, pf: PayoffFacts) -> str:
    c = p.plans[p.recommended]
    if p.recommended == "cash":
        first = "Paying {price} in cash for the {car} " + ("skips about {interest_avoided} in loan interest." if "interest_avoided" in pf.values else "means no loan.")
    elif p.recommended == "mix":
        first = "Putting {down_payment} down on the {car} and financing the rest means a {payment} monthly payment and about {total_interest} in interest."
    else:
        first = "Financing the {car} means a {payment} monthly payment and about {total_interest} in interest."
    second = ""
    if "payment_over" in pf.mentions and "cushion_short" in pf.mentions:
        second = " The payment is {payment_share} of your gross income, over the {payment_limit_share} line, and in a bad stretch your cash would fall {cushion_shortfall} under your cushion target."
    elif "payment_over" in pf.mentions:
        second = " The payment is {payment_share} of your gross income, over the {payment_limit_share} line."
    elif "cushion_short" in pf.mentions:
        second = " In a bad stretch your cash would fall {cushion_shortfall} under your cushion target."
    return (first + second).strip()


def template_text(p: BuyPlans, pf: PayoffFacts) -> str:
    return tidy(grounding.fill(template_source(p, pf), pf.text))


def model_view(p: BuyPlans, pf: PayoffFacts) -> str:
    lines = ["Question: how should I pay for this car? (The app answers that separately; you do not.)",
             f"The way to pay: {p.plans[p.recommended].name}.", "Placeholders you may use (write them exactly, with the braces):"]
    lines += [f"{{{k}}} = {v}" for k, v in pf.means.items()]
    notes = {"payment_over": "the payment is above the share of gross income a car payment should stay under",
             "cushion_short": "in a bad stretch your cash would fall under the safety cushion"}
    if pf.mentions:
        lines.append("Be sure to mention: " + "; ".join(f"{notes[k]} (use {', '.join('{' + n + '}' for n in names)})" for k, names in pf.mentions.items()) + ".")
    lines.append("Example (adapt it, keeping the braces): " + template_source(p, pf))
    return "\n".join(lines)


def _template_answer(p: BuyPlans, pf: PayoffFacts, reason: str, t: _Tally) -> PayoffAnswer:
    t.first_reasons, t.final_reasons = t.first_reasons or [reason], t.final_reasons or [reason]
    return _answer(template_text(p, pf), "template", t)


async def answer(p: BuyPlans, pf: PayoffFacts, budget_s: float | None = None) -> PayoffAnswer:
    limit = llm_config.advisor_budget_s() if budget_s is None else budget_s
    tally = _Tally()
    try:
        async with asyncio.timeout(None if limit == math.inf else limit):
            return await _with_model(p, pf, tally)
    except TimeoutError:
        return _template_answer(p, pf, "budget_exceeded", _Tally(retried=tally.retried, calls=tally.calls))


async def _with_model(p: BuyPlans, pf: PayoffFacts, t: _Tally) -> PayoffAnswer:
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": model_view(p, pf)}]
    ask = lambda msgs, **kw: client.complete("advisor", msgs, json_schema=AnswerModel, max_tokens=ADVISOR_MAX_TOKENS, schema_in_prompt=False,
                                             response_schema=RESPONSE_SCHEMA, **kw)
    try:
        first = await ask(messages)
    except client.LLMUnavailable:
        return _template_answer(p, pf, "llm_unavailable", t)
    except client.LLMError:
        return _template_answer(p, pf, "llm_bad_reply", t)
    t.calls = 1
    text, reasons, report = check_answer(first.parsed.answer, pf, p.verdict)
    t.first_ok, t.first_reasons = not reasons, reasons
    if reasons:
        t.retried = True
        complaint = ("Problems: " + ", ".join(reasons).replace("_", " ") + ". Write at most two plain sentences, adapting the example above, "
                     "using the listed {placeholders} (and each point under \"Be sure to mention\"), with no judgment. Reply with only the JSON.")
        try:
            retry = await ask(messages + [{"role": "assistant", "content": first.content}, {"role": "user", "content": complaint}],
                              temperature=grounding.RETRY_TEMPERATURE)
            t.calls = 2
            text, reasons, report = check_answer(retry.parsed.answer, pf, p.verdict)
            t.final_reasons = reasons
        except client.LLMError as e:
            t.final_reasons = reasons = ["llm_unavailable" if isinstance(e, client.LLMUnavailable) else "llm_bad_reply"]
    if reasons:
        return _answer(template_text(p, pf), "template", t)
    t.checked = len(report.numbers)
    return _answer(text, "model", t)


def no_options_answer() -> PayoffAnswer:
    """No car fits: a fixed sentence, no model call."""
    t = _Tally(first_ok=True)
    t.first_reasons = t.final_reasons = ["no_options"]
    return _answer("No car in the research pack fits that segment and budget, so there is nothing to compare yet.", "template", t)
