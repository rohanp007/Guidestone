"""The advisor (docs/rules.md section 4): picks and words the next moves. It never decides them.

Code decides everything that matters: the verdict (rules.verdict), the candidate moves and their facts (rules.candidate_moves). The
model only chooses up to 3 of the candidates, orders them, and writes a short title and one sentence for each, using {placeholders}
that the app fills with the real figures.

Checks, in order: every move_code must be a candidate (an unknown one is rejected and retried); then each move's text, after the
placeholders are filled, goes through the grounding checks (figures against that move's own facts, wording against the forecast
labels, no judgments, plain English). Anything wrong -> one retry at temperature 0.4 that names the problems -> for the moves that are
still wrong, the deterministic template for their move_code (and if nothing usable is left, the top 3 candidates in waterfall order).
The model's view has move codes (it must name them) but no numbers and no verdict wording to repeat.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..data.models import Profile
from ..formulas import rules
from ..llm import client, grounding
from ..llm.facts import CUSHION_TEXT, OVERDRAFT_TEXT, TREND_TEXT
from .models import Advice, AdviceMove, GroundingSummary

ADVISE_VERSION = "advise-v1"
MAX_MOVES = 3
ADVISOR_MAX_TOKENS = 1500           # the advisor reasons first; a small budget cuts the JSON off
VERDICT_TEXT = {"act_now": "act now", "plan_ahead": "plan ahead", "on_track": "on track"}


# ---- what the model writes -----------------------------------------------------------------------------------------------------

class AdvisorMove(BaseModel):
    move_code: str = Field(description="Exactly one of the candidate move codes")
    title_text: str = Field(description="A few words, using {placeholders} for any figure")
    why_text: str = Field(description="One short plain sentence for this customer, using {placeholders} for every figure")


class AdvisorAnswer(BaseModel):
    moves: list[AdvisorMove] = Field(default_factory=list, description="At most 3, most important first")


ADVISOR_PROMPT = (
    "You are a careful financial coach. From the candidate moves, choose up to 3 that matter most for this customer, most important "
    "first. For each, write a short title (a few words, capitalised) and one plain sentence saying why it fits this customer.\n"
    "Rules:\n"
    "- move_code must be copied exactly from the list. Choose only from the list.\n"
    "- Each move has an example sentence. Adapt it: keep its {placeholders} written in braces exactly, because the app fills in the "
    "real amounts, dates and percentages. Never type a number yourself, and use only the placeholders listed under that same move.\n"
    "- The moves are listed in the app's suggested order; keep that order unless this customer's situation clearly calls for another.\n"
    "- Do not state an overall judgment, risk level or reassurance, and do not repeat the urgency. The app shows that separately.\n"
    "- Plain English only. No codes or field names in the title or sentence.\n"
    'Reply with only JSON: {"moves":[{"move_code":"<code>","title_text":"<few words>","why_text":"<one sentence>"}]}')


# ---- the move catalogue --------------------------------------------------------------------------------------------------------
# Per move_code: the placeholders it offers (from the candidate's facts), what the model is told each means, the blurb the model
# sees, and the deterministic template. A placeholder's NAME ends the way grounding expects for its kind: _apr / _rate / _pct / _share
# are percentages, _months are lengths of time, everything else is money or a date.

def usd(x: float) -> str:
    return f"${abs(x):,.0f}"


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def months(x: float) -> str:
    return f"{int(x)} months"


def day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def plain(x: Any) -> str:
    return f"{x:g}"


@dataclass(frozen=True)
class P:
    name: str
    get: Callable[[dict], Any]
    fmt: Callable[[Any], str]
    means: str


@dataclass(frozen=True)
class Spec:
    blurb: str | Callable[[dict], str]
    ph: tuple[P, ...]
    title: str
    why: str | Callable[[dict], str]


SPECS: dict[str, Spec] = {
    "close_monthly_gap": Spec(
        "Spending is higher than income every month, so closing that gap comes before any other goal.",
        (P("monthly_gap", lambda f: f["monthly_gap"], usd, "how much more goes out than comes in each month"),
         P("monthly_essentials", lambda f: f["essentials_monthly"], usd, "monthly essential costs")),
        "Close your monthly gap",
        "You spend about {monthly_gap} a month more than comes in, so closing that gap comes before any other goal."),
    "build_starter_cushion": Spec(
        "Cash on hand is below a small starter emergency fund.",
        (P("starter_cushion_now", lambda f: f["liquid"], usd, "cash on hand today"),
         P("starter_cushion_target", lambda f: f["target"], usd, "size of the starter fund"),
         P("starter_cushion_gap", lambda f: f["gap"], usd, "how much more is needed")),
        "Build a starter cushion",
        "You have {starter_cushion_now} in cash against a starter fund of {starter_cushion_target}; adding {starter_cushion_gap} gets you there."),
    "confirm_401k_match": Spec(
        "The customer's employer offers a 401(k) match, and we cannot see whether they get all of it.",
        (P("match_pct", lambda f: f["employer_match_pct"], pct, "share of pay the employer matches"),
         P("match_monthly", lambda f: f["monthly_to_get_match"], usd, "what the full match is worth each month")),
        "Confirm your 401(k) match",
        "Your employer matches up to {match_pct} of your pay, worth about {match_monthly} a month, so check that you contribute enough to get all of it."),
    "pay_card_high_apr": Spec(
        "A credit card charges high interest, more than savings can earn.",
        (P("card_balance", lambda f: f["total_balance"], usd, "balance owed on high-interest cards"),
         P("card_apr", lambda f: f["highest_apr"], pct, "the highest yearly interest rate")),
        "Pay down your high-interest card",
        "Your card balance of {card_balance} costs {card_apr} a year in interest, so extra money goes here before other goals."),
    "pay_loan_high_apr": Spec(
        "A loan charges high interest, more than savings can earn.",
        (P("loan_balance", lambda f: f["total_balance"], usd, "balance owed on high-interest loans"),
         P("loan_apr", lambda f: f["highest_apr"], pct, "the highest yearly interest rate")),
        "Pay down your high-interest loan",
        "Your loan balance of {loan_balance} costs {loan_apr} a year in interest, so extra money goes here before other goals."),
    "build_full_cushion": Spec(
        "Cash on hand is below a full emergency cushion.",
        (P("cushion_now", lambda f: f["liquid"], usd, "cash on hand today"),
         P("cushion_target", lambda f: f["target"], usd, "size of the full cushion"),
         P("cushion_gap", lambda f: f["gap"], usd, "how much more is needed"),
         P("cushion_months", lambda f: f["months"], months, "how many months of essentials the cushion covers, with the unit")),
        "Build your full emergency cushion",
        "You have {cushion_now} in cash against a full cushion of {cushion_target} ({cushion_months} of essentials), so {cushion_gap} more is needed."),
    "increase_retirement": Spec(
        "Retirement saving is below the usual target share of pay.",
        (P("retirement_monthly_now", lambda f: f["current_monthly"], usd, "retirement saving each month today"),
         P("retirement_monthly_target", lambda f: f["target_monthly"], usd, "the target each month"),
         P("retirement_monthly_gap", lambda f: f["gap_monthly"], usd, "how much more each month")),
        "Save more for retirement",
        "You save about {retirement_monthly_now} a month toward retirement against a target of {retirement_monthly_target}, so {retirement_monthly_gap} more a month closes the gap."),
    "start_education_savings": Spec(
        "The customer has children under 18 and the earlier goals are under way. Retirement saving comes first.",
        (),
        "Start saving for education",
        "Retirement saving comes first; once it is on track, start setting money aside for your children's education."),
    "start_credit_building": Spec(
        "The credit score is low and there is no open credit card.",
        (P("credit_score", lambda f: f["credit_score"], plain, "the current credit score"),),
        "Start building your credit",
        "With a credit score of {credit_score}, one card used lightly and paid in full each month can help build it."),
    "move_idle_cash_to_hys": Spec(
        "More cash is sitting in everyday accounts than needed.",
        (P("cash_total", lambda f: f["liquid"], usd, "total cash in checking and savings"),
         P("monthly_essentials", lambda f: f["essentials_monthly"], usd, "monthly essential costs"),
         P("savings_rate", lambda f: f["safe_savings_rate"], pct, "yearly rate a high-yield savings account pays")),
        "Put idle cash to work",
        "You hold {cash_total} in cash, well beyond a few months of the {monthly_essentials} a month you need for essentials; a high-yield savings account pays about {savings_rate}."),
    "cd_renewal_review": Spec(
        "A certificate of deposit matures soon.",
        (P("cd_balance", lambda f: f["balance"], usd, "the certificate's balance"),
         P("cd_rate", lambda f: f["rate"], pct, "the certificate's yearly rate"),
         P("cd_matures", lambda f: f["matures"], day, "the date it matures")),
        "Review your CD before it renews",
        "Your CD of {cd_balance} at {cd_rate} matures on {cd_matures}; compare rates before it renews."),
    "use_extra_paycheck": Spec(
        "An extra paycheck is coming.",
        (P("extra_paycheck_date", lambda f: f["date"], day, "the date of the extra paycheck"),),
        "Plan for your extra paycheck",
        "You get an extra paycheck on {extra_paycheck_date}; decide now where that money goes."),
    "recently_freed_payment": Spec(
        lambda f: ("A loan payment recently ended, freeing up money each month." if f["freed"][0]["state"] == "ended"
                   else "A loan payment is about to end, freeing up money each month."),
        (P("freed_date", lambda f: f["freed"][0]["ends"], day, "the date the loan payment ended or ends"),),
        "Redirect your freed-up payment",
        lambda f: ("A loan payment ended on {freed_date}; point that money at your next goal before it gets absorbed."
                   if f["freed"][0]["state"] == "ended" else
                   "A loan payment ends on {freed_date}; point that money at your next goal before it gets absorbed.")),
    "automate_savings": Spec(
        "Money is left over each month, and little of it is moved to savings automatically.",
        (P("monthly_surplus", lambda f: f["surplus"], usd, "what is left over each month"),
         P("automated_monthly", lambda f: f["automated_monthly"], usd, "what is already moved to savings automatically each month")),
        "Automate your savings",
        "About {monthly_surplus} a month is left over and only {automated_monthly} is moved to savings automatically; automating more makes saving happen first."),
}

# Which placeholders are percentages / counts / dates is carried by the name (see above); the grounding facts use these exact names.
_COUNT_UNIT = re.compile(r"\b(months?)\s+months?\b", re.I)


def tidy(text: str) -> str:
    """fill() fixes doubled % and $; the placeholders for lengths of time carry their unit too ("6 months"), so fix "6 months months"."""
    return _COUNT_UNIT.sub(r"\1", text)


@dataclass
class MoveText:
    code: str
    ph: dict[str, str]                 # name -> text the customer reads
    values: dict[str, Any]             # name -> raw value, for the grounding check
    means: dict[str, str]


def move_text(code: str, facts: dict) -> MoveText:
    spec = SPECS[code]
    ph, values = {}, {}
    for p in spec.ph:
        raw = p.get(facts)
        ph[p.name], values[p.name] = p.fmt(raw), raw
    return MoveText(code, ph, values, {p.name: p.means for p in spec.ph})


def template_move(code: str, facts: dict) -> tuple[str, str]:
    """The deterministic title and sentence for a move: the catalogue text with its own placeholders filled in."""
    spec, mt = SPECS[code], move_text(code, facts)
    why = spec.why(facts) if callable(spec.why) else spec.why
    return tidy(grounding.fill(spec.title, mt.ph)), tidy(grounding.fill(why, mt.ph))


# ---- inputs --------------------------------------------------------------------------------------------------------------------

@dataclass
class Inputs:
    """What the formulas decided, before any model is involved."""
    waterfall: rules.Waterfall
    moves: rules.CandidateMoves
    verdict: str
    candidates: dict[str, rules.CandidateMove]       # unique by move_code, in waterfall order (the first of each code wins)
    labels: dict[str, str]                           # the forecast labels, for the wording checks


def prepare(profile: Profile, facts: dict) -> Inputs:
    """Run the rule formulas. Synchronous; call it off the event loop."""
    wf = rules.priority_waterfall(profile)
    moves = rules.candidate_moves(profile)
    v = rules.verdict(profile, facts["overall_status"], facts.get("chance_of_overdraft", 0.0), wf)
    candidates: dict[str, rules.CandidateMove] = {}
    for m in moves.moves:
        if m.move_code in SPECS:
            candidates.setdefault(m.move_code, m)
    labels = {k: facts[k] for k in ("cushion_status", "trend", "overdraft_status", "overall_status") if k in facts}
    return Inputs(wf, moves, v, candidates, labels)


# ---- the model's view ----------------------------------------------------------------------------------------------------------

def model_view(inp: Inputs, facts: dict) -> str:
    """Plain language, the codes the model must name, and placeholders. No figures, no status codes."""
    lines = [
        f"Customer: {'retired' if facts.get('employment') == 'retired' else 'employed'}"
        f"{', often runs low before payday' if facts.get('paycheck_to_paycheck') == 'yes' else ''}.",
        f"Cash outlook: {TREND_TEXT.get(facts.get('trend'), 'unclear')}; it {CUSHION_TEXT.get(facts.get('cushion_status'), '')}; "
        f"{OVERDRAFT_TEXT.get(facts.get('overdraft_status'), '')}.",
        f"Urgency set by the app (do not repeat it): {VERDICT_TEXT[inp.verdict]}.",
        "Candidate moves, in the app's suggested order:"]
    for i, (code, move) in enumerate(inp.candidates.items(), 1):
        mt = move_text(code, move.facts)
        spec = SPECS[code]
        lines.append(f"{i}. move_code {code}: {spec.blurb(move.facts) if callable(spec.blurb) else spec.blurb}")
        lines.append("   placeholders: " + ("; ".join(f"{{{k}}} = {v}" for k, v in mt.means.items()) or "none (write no figures)"))
        lines.append(f"   example sentence: {spec.why(move.facts) if callable(spec.why) else spec.why}")
    return "\n".join(lines)


# ---- checking ------------------------------------------------------------------------------------------------------------------

@dataclass
class Checked:
    move: AdvisorMove                  # with the placeholders filled in
    report: grounding.GroundingReport
    no_figures: bool = False           # the move has figures to give, and the sentence uses none of them

    @property
    def ok(self) -> bool:
        return self.report.ok and not self.no_figures


def _fill_all(inp: Inputs) -> dict[str, str]:
    out: dict[str, str] = {}
    for code, move in inp.candidates.items():
        out |= move_text(code, move.facts).ph
    return out


def check_move(m: AdvisorMove, inp: Inputs) -> Checked:
    """Fill the placeholders, then check the words against ONLY this move's own facts (a figure from another move is not allowed)."""
    move = inp.candidates[m.move_code]
    mt = move_text(m.move_code, move.facts)
    allp = _fill_all(inp)
    filled = AdvisorMove(move_code=m.move_code, title_text=tidy(grounding.fill(m.title_text, allp) or ""),
                         why_text=tidy(grounding.fill(m.why_text, allp) or ""))
    facts = {**mt.values, **inp.labels}
    report = grounding.check(f"{filled.title_text}. {filled.why_text}", facts, judgments=True, style=True, hints=False)
    unused = bool(mt.ph) and not any("{" + k + "}" in m.why_text for k in mt.ph)           # a move with figures must show at least one
    return Checked(filled, report, unused)


def required(inp: Inputs) -> list[str]:
    """Moves that serve the waterfall's current step must be among the advice: the model decides wording and order, not whether the
    most pressing step is mentioned."""
    return [code for code, m in inp.candidates.items() if m.addresses_current_step]


def _complaint(invalid: list[str], bad: list[Checked], inp: Inputs, missing: list[str]) -> str:
    parts = []
    if missing:
        parts.append(f"You must include: {', '.join(missing)}.")
    if invalid:
        parts.append(f"These move codes are not in the list: {', '.join(invalid)}. Use only: {', '.join(inp.candidates)}.")
    for c in bad:
        r, bits = c.report, []
        if r.unmatched:
            bits.append("figures not in its facts: " + ", ".join(n.text for n in r.unmatched))
        wording = [x for x in r.conflicts if x.field not in ("judgment", "style")]
        if wording:
            bits.append("contradicts the facts: " + "; ".join(f'"{x.phrase}"' for x in wording))
        if any(x.field == "judgment" for x in r.conflicts):
            bits.append("states a judgment or reassurance, which the app shows separately: "
                        + "; ".join(f'"{x.phrase}"' for x in r.conflicts if x.field == "judgment"))
        if c.no_figures:
            bits.append("it uses none of its {placeholders}; adapt the example sentence and keep its placeholders")
        if any(x.field == "style" for x in r.conflicts):
            bits.append("not plain English; use only the listed {placeholders}: " + "; ".join(f'"{x.phrase}"' for x in r.conflicts if x.field == "style"))
        parts.append(f"For {c.move.move_code}: " + "; ".join(bits) + ".")
    return " ".join(parts) + " Rewrite the answer, fixing only these problems, and keep the JSON format."


def _screen(answer: AdvisorAnswer, inp: Inputs) -> tuple[list[Checked], list[str], list[Checked]]:
    """(the valid, distinct moves in the model's order, up to MAX_MOVES; unknown codes; those among them that failed the checks)."""
    seen, good, invalid = set(), [], []
    for m in answer.moves:
        if m.move_code not in inp.candidates:
            invalid.append(m.move_code[:40])
        elif m.move_code not in seen and len(good) < MAX_MOVES:
            seen.add(m.move_code)
            good.append(check_move(m, inp))
    return good, invalid, [c for c in good if not c.ok]


# ---- the run -------------------------------------------------------------------------------------------------------------------

@dataclass
class _Tally:
    checked: int = 0
    retried: bool = False
    calls: list[client.LLMResult] = field(default_factory=list)


def _ordered(moves: list[AdviceMove]) -> list[AdviceMove]:
    """Retirement before education, always (docs/rules.md section 1)."""
    codes = [m.move_code for m in moves]
    if "increase_retirement" in codes and "start_education_savings" in codes and codes.index("start_education_savings") < codes.index("increase_retirement"):
        i, j = codes.index("start_education_savings"), codes.index("increase_retirement")
        moves[i], moves[j] = moves[j], moves[i]
    return moves


def _assemble(inp: Inputs, picked: list[tuple[str, str, str, str]], tally: _Tally, ok: bool) -> Advice:
    """picked: (move_code, title, why, source)."""
    moves = _ordered([AdviceMove(rank=0, move_code=c, title_text=t, why_text=w, source=s, priority=inp.candidates[c].priority,
                                 addresses_current_step=inp.candidates[c].addresses_current_step) for c, t, w, s in picked])
    for i, m in enumerate(moves, 1):
        m.rank = i
    sources = {m.source for m in moves}
    notes = []
    if "increase_retirement" in inp.candidates and "start_education_savings" in inp.candidates:
        notes.append("Retirement saving comes before education saving.")
    return Advice(verdict=inp.verdict, current_step=inp.waterfall.current_step, moves=moves, notes=notes,
                  source="model" if sources == {"model"} else "template" if sources <= {"template"} else "mixed",
                  grounding=GroundingSummary(ok=ok, checked=tally.checked, retried=tally.retried))


def template_advice(inp: Inputs, tally: _Tally | None = None) -> Advice:
    """The model's absence: the top candidates in waterfall order with template text."""
    tally = tally or _Tally()
    picked = []
    for code, move in list(inp.candidates.items())[:MAX_MOVES]:
        title, why = template_move(code, move.facts)
        picked.append((code, title, why, "template"))
    return _assemble(inp, picked, tally, ok=True)


async def advise(inp: Inputs, facts: dict) -> Advice:
    """The model's choice, checked; the template wherever the model's words fail. Never raises because the model failed."""
    if not inp.candidates:
        return _assemble(inp, [], _Tally(), ok=True)
    tally = _Tally()
    messages = [{"role": "system", "content": ADVISOR_PROMPT}, {"role": "user", "content": model_view(inp, facts)}]
    try:
        first = await client.complete("advisor", messages, json_schema=AdvisorAnswer, max_tokens=ADVISOR_MAX_TOKENS, schema_in_prompt=False)
    except client.LLMError:
        return template_advice(inp, tally)
    tally.calls.append(first)
    good, invalid, bad = _screen(first.parsed, inp)
    answer = first
    missing = lambda: [c for c in required(inp) if c not in {g.move.move_code for g in good}]
    if invalid or bad or not good or missing():
        tally.retried = True
        complaint = _complaint(invalid, bad, inp, missing()) if (invalid or bad or good) else (
            f"You chose no move. Choose up to {MAX_MOVES} from: {', '.join(inp.candidates)}.")
        try:
            answer = await client.complete("advisor", messages + [{"role": "assistant", "content": first.content},
                                                                  {"role": "user", "content": complaint}],
                                           json_schema=AdvisorAnswer, max_tokens=ADVISOR_MAX_TOKENS, schema_in_prompt=False,
                                           temperature=grounding.RETRY_TEMPERATURE)
            tally.calls.append(answer)
            good, invalid, bad = _screen(answer.parsed, inp)
        except client.LLMError:
            good, bad = [], []
    tally.checked = sum(len(c.report.numbers) for c in good)
    if not good:                                                   # nothing usable from the model: top candidates, template text
        return template_advice(inp, tally)
    failed = {c.move.move_code for c in bad}
    picked = [(c.move.move_code, c.move.title_text, c.move.why_text, "model") if c.move.move_code not in failed else
              (c.move.move_code, *template_move(c.move.move_code, inp.candidates[c.move.move_code].facts), "template") for c in good]
    for code in reversed(missing()):                               # still missing after the retry: the template, first
        picked.insert(0, (code, *template_move(code, inp.candidates[code].facts), "template"))
    picked = picked[:MAX_MOVES]
    return _assemble(inp, picked, tally, ok=True)           # whatever is shown passed the checks, or is a template built from the facts
