"""The Researcher: from the research pack, up to three cars that fit a sanitized query.

Input to the model: only the BuyQuery (segment, budget band, priorities) and the matching pack entries. No profile field, no APR. It
returns ids (an enum in the response schema) and one sentence each, written with {placeholders}; the app fills in the cited figures, and
every figure in the filled sentence must exist in that car's own record (grounding). One retry at a higher temperature, then the
fallback: the top three by a code score on the priorities, with a template sentence."""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..llm import client, grounding
from ..research import pack
from ..research.sanitize import BuyQuery
from .advisor import tidy, usd
from .models import ResearchPick, SourceLine

MAX_PICKS = 3

PROMPT = (
    "You are a careful car researcher. From the cars listed, pick up to three that best fit the customer's segment, budget and priorities. "
    "For each pick write ONE short plain sentence about why it fits.\n"
    "Rules:\n"
    "- Use the {placeholders} for every price, mileage or rating, written in braces exactly; the app fills in the real figures. "
    "Never type a number yourself. Use only the placeholders listed for that car.\n"
    "- Never mention the customer's budget or any figure that is not one of that car's placeholders.\n"
    "- Do not say it is the best, do not promise savings, do not give advice about paying for it.\n"
    '- Reply with only JSON: {"picks":[{"id":"<car id>","why":"<one sentence>"}]}')


class PickModel(BaseModel):
    id: str
    why: str = Field(description="One short sentence using the placeholders for every figure")


class PicksModel(BaseModel):
    picks: list[PickModel]


def response_schema(ids: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["picks"], "properties": {"picks": {
        "type": "array", "minItems": 1, "maxItems": MAX_PICKS, "items": {"type": "object", "additionalProperties": False, "required": ["id", "why"],
        "properties": {"id": {"type": "string", "enum": ids}, "why": {"type": "string"}}}}}}


# ---- a car's figures --------------------------------------------------------------------------------------------------------------

@dataclass
class Facts:
    values: dict[str, float]            # numeric figures for the grounding check
    text: dict[str, str]                # placeholder -> what the customer reads
    means: dict[str, str]


def facts_for(v: dict) -> Facts:
    values, text, means = {}, {}, {}
    if (x := v["msrp"]["value"]) is not None:
        values["msrp"], text["price"], means["price"] = x, usd(x), "the starting price"
    if (x := v["mpg_combined"]["value"]) is not None:
        values["mpg_combined"], text["mpg"], means["mpg"] = x, f"{x} mpg", "the combined fuel economy, with the unit"
    if (x := v["nhtsa_stars"]["value"]) is not None:
        values["nhtsa_stars"], text["stars"], means["stars"] = x, f"{x} stars", "the NHTSA overall safety rating, as in '5 stars'"
    if (x := v["iihs_award"]["value"]) is not None:
        text["award"], means["award"] = x, "the IIHS safety award name, as in 'Top Safety Pick+'"
    return Facts(values, text, means)


def sources_for(v: dict) -> list[SourceLine]:
    return [SourceLine(figure=f, **pack.source_line(v[f])) for f in pack.FIGURES if v[f]["value"] is not None]


# ---- the fallback: a code score -----------------------------------------------------------------------------------------------------

def score(v: dict, entries: list[dict], priorities: tuple[str, ...]) -> float:
    prices = [pack.price(e) for e in entries]
    mpgs = [e["mpg_combined"]["value"] or 0 for e in entries]
    span = lambda x, xs: (x - min(xs)) / ((max(xs) - min(xs)) or 1)
    parts = {"price": 1 - span(pack.price(v), prices), "mpg": span(v["mpg_combined"]["value"] or 0, mpgs),
             "safety": ((v["nhtsa_stars"]["value"] or 0) / 5 + (2 if (v["iihs_award"]["value"] or "").endswith("+") else 1 if v["iihs_award"]["value"] else 0) / 2) / 2}
    order = priorities or ("price",)
    return sum(parts[p] * (len(order) - i) for i, p in enumerate(order)) + 0.001 * parts["price"]


def top_by_score(entries: list[dict], q: BuyQuery) -> list[dict]:
    return sorted(entries, key=lambda v: (-score(v, entries, q.priorities), v["id"]))[:MAX_PICKS]


def template_why(v: dict, q: BuyQuery) -> str:
    f = facts_for(v)
    bits = ["starts at {price}"]
    if "mpg" in f.text:
        bits.append("gets {mpg} combined")
    if "stars" in f.text and "safety" in q.priorities:
        bits.append("is rated {stars} overall by NHTSA")
    return v["name"] + " " + (", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0]) + "."


# ---- checking and picking ----------------------------------------------------------------------------------------------------------

def check_why(raw: str, v: dict) -> tuple[str, list[str]]:
    """(the filled sentence, reasons it failed). Every figure must be one of this car's own."""
    f = facts_for(v)
    filled = tidy(grounding.fill(raw, f.text) or "")
    rep = grounding.check(filled, f.values, judgments=True, style=True, hints=False)
    reasons = ["figure_not_in_facts"] if rep.unmatched else []
    reasons += [c.field if c.field in ("judgment", "style") else f"contradicts_{c.field}" for c in rep.conflicts]
    if f.text and not any("{" + n + "}" in raw for n in f.text):
        reasons.append("no_placeholder")
    if len([s for s in re.split(r"(?<=[.!?])\s+", filled.strip()) if s]) > 2:
        reasons.append("too_long")
    return filled, list(dict.fromkeys(reasons))


def view(q: BuyQuery, entries: list[dict]) -> str:
    lines = [f"Segment: {q.segment.replace('_', ' ')}", f"Budget: {'up to ' + usd(q.budget_band) if q.budget_band else 'no limit given'}",
             f"Priorities, in order: {', '.join(q.priorities) or 'none given'}", "", "Cars (id | placeholders for that car; values for your choosing only):"]
    for e in entries:
        f = facts_for(e)
        lines.append(f"- {e['id']} | " + "; ".join(f"{{{k}}} = {f.text[k]} ({f.means[k]})" for k in f.text))
    ex = entries[0]
    lines += ["", "Example of one sentence (adapt it; keep the braces): " + template_why(ex, q).replace(ex["name"], "It", 1)]
    return "\n".join(lines)


def _pick(v: dict, why: str, source: str) -> ResearchPick:
    return ResearchPick(id=v["id"], name=v["name"], why_text=why, source=source, sources=sources_for(v))  # type: ignore[arg-type]


@dataclass
class Research:
    picks: list[ResearchPick]
    source: str                         # "model" or "template"
    retried: bool = False
    reasons: tuple[str, ...] = ()


async def research(q: BuyQuery) -> Research:
    entries = pack.eligible(q.segment, q.budget_band)
    if not entries:
        return Research([], "template", reasons=("no_cars",))
    by_id = {e["id"]: e for e in entries}
    fallback = lambda why: Research([_pick(v, check_why(template_why(v, q), v)[0], "template") for v in top_by_score(entries, q)], "template", reasons=(why,))
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": view(q, entries)}]
    ask = lambda msgs, **kw: client.complete("researcher", msgs, json_schema=PicksModel, schema_in_prompt=False,
                                             response_schema=response_schema(list(by_id)), **kw)

    def verify(parsed: PicksModel) -> tuple[list[ResearchPick] | None, list[str]]:
        picks, reasons, seen = [], [], set()
        for p in parsed.picks[:MAX_PICKS]:
            if p.id not in by_id or p.id in seen:
                reasons.append("unknown_id")
                continue
            seen.add(p.id)
            text, bad = check_why(p.why, by_id[p.id])
            reasons += bad
            picks.append(_pick(by_id[p.id], text, "model"))
        return (picks if picks and not reasons else None), reasons

    try:
        first = await ask(messages)
    except client.LLMError:
        return fallback("llm_unavailable")
    picks, reasons = verify(first.parsed)
    if picks:
        return Research(picks, "model")
    try:
        retry = await ask(messages + [{"role": "assistant", "content": first.content},
                                      {"role": "user", "content": "Problems: " + ", ".join(dict.fromkeys(reasons)).replace("_", " ") +
                                       ". Pick from the listed ids, one sentence each, using only that car's {placeholders}. Reply with only the JSON."}],
                          temperature=grounding.RETRY_TEMPERATURE)
    except client.LLMError:
        r = fallback("llm_unavailable"); r.retried = True; return r
    picks, reasons = verify(retry.parsed)
    if picks:
        return Research(picks, "model", retried=True)
    r = fallback(reasons[0] if reasons else "llm_bad_reply"); r.retried = True
    return r
