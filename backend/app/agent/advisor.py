"""The advisor (docs/rules.md section 4): picks and words the next moves. It never decides them.

Code decides everything that matters: the verdict (rules.verdict), the candidate moves and their facts (rules.candidate_moves). The
model only chooses up to 3 of the candidates, orders them, and writes one sentence for each, using {placeholders} that the app
fills with the real figures. Titles are fixed per move code (sentence case), and a zero figure that would read badly ("save $0 a month")
has a code-chosen wording of its own.

Checks, in order: every move_code must be a candidate (an unknown one is rejected and retried); then each move's text, after the
placeholders are filled, goes through the grounding checks (figures against that move's own facts, wording against the forecast
labels, no judgments, plain English). Anything wrong -> one retry at temperature 0.4 that names the problems -> for the moves that are
still wrong, the deterministic template for their move_code (and if nothing usable is left, the top 3 candidates in waterfall order).
The whole call has a time budget (config advisor_budget_s); past it, the template result is served, never an error.
The model's view has move codes (it must name them) but no numbers and no verdict wording to repeat.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date
import math
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..data.models import Profile
from ..formulas import rules
from ..llm import client, config as llm_config, grounding
from ..llm.facts import CUSHION_TEXT, OVERDRAFT_TEXT, TREND_TEXT
from .models import Advice, AdviceDiagnostics, AdviceMove, BadReply, GroundingSummary

ADVISE_VERSION = "advise-v1"
MAX_MOVES = 3
ADVISOR_MAX_TOKENS = 1500           # the advisor reasons first; a small budget cuts the JSON off
VERDICT_TEXT = {"act_now": "act now", "plan_ahead": "plan ahead", "on_track": "on track"}


# ---- what the model writes -----------------------------------------------------------------------------------------------------

class AdvisorMove(BaseModel):
    move_code: str = Field(description="Exactly one of the candidate move codes")
    why_text: str = Field(description="One short plain sentence for this customer, using {placeholders} for every figure")


class AdvisorAnswer(BaseModel):
    """The app puts the move for the current step first (`pinned_why` is its sentence); the model picks up to 2 more (3 when nothing is
    pinned) from the other candidates."""
    pinned_why: str | None = Field(None, description="The sentence for the first move, which the app has chosen")
    more: list[AdvisorMove] = Field(default_factory=list, description="Further moves, most important first")


def response_schema(inp: "Inputs") -> dict:
    """The JSON Schema the server is asked to constrain its output to: move_code can only be one of THIS customer's other candidates,
    there are at most 2 of them (3 when nothing is pinned), and the pinned sentence is required."""
    pinned, rest = pinned_code(inp), [c for c in inp.candidates if c != pinned_code(inp)]
    item = {"type": "object", "additionalProperties": False, "required": ["move_code", "why_text"],
            "properties": {"move_code": {"type": "string", "enum": rest}, "why_text": {"type": "string"}}}
    props: dict = {}
    if pinned:
        props["pinned_why"] = {"type": "string"}
    if rest:
        props["more"] = {"type": "array", "maxItems": MAX_MOVES - bool(pinned), "items": item}
    return {"type": "object", "additionalProperties": False, "properties": props,
            "required": (["pinned_why"] if pinned else []) + ([] if pinned or not rest else ["more"])}


ADVISOR_PROMPT = (
    "You are a careful financial coach. The app has already chosen the customer's first move. You write a plain sentence saying why "
    "it fits this customer (two sentences if its example has two: keep both), and you may add up to 2 more moves from the others, most "
    "important first, each with exactly one sentence.\n"
    "Rules:\n"
    "- move_code must be copied exactly from the list of other moves. Choose only from that list.\n"
    "- Each move has an example sentence. Adapt it: keep its {placeholders} written in braces exactly, because the app fills in the "
    "real amounts, dates and percentages. Never type a number yourself, and use only the placeholders listed under that same move.\n"
    "- The other moves are listed in the app's suggested order; keep that order unless this customer's situation clearly calls for another.\n"
    "- Do not state an overall judgment, risk level or reassurance, and do not repeat the urgency. The app shows that separately.\n"
    "- Plain English only. No codes or field names in the sentences.\n"
    'Reply with only JSON: {"pinned_why":"<sentence for the first move>","more":[{"move_code":"<code>","why_text":"<sentence>"}]}. '
    "When there is no first move, leave out pinned_why and choose 1 to 3 moves.")


# ---- the move catalogue --------------------------------------------------------------------------------------------------------
# Per move_code: the placeholders it offers (from the candidate's facts), what the model is told each means, the blurb the model
# sees, and the deterministic template. A placeholder's NAME ends the way grounding expects for its kind: _apr / _rate / _pct / _share
# are percentages, _months are lengths of time, everything else is money or a date.

ZERO = 0.5                           # a money figure under this shows as "$0"
ZERO_FIGURE = re.compile(r"\$0(?!\d|,\d|\.\d)")


def usd(x: float) -> str:
    return f"${abs(x):,.0f}"


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def months(x: float) -> str:
    return f"{int(x)} month{'' if int(x) == 1 else 's'}"


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
    title: str                                       # fixed, sentence case; the model never writes titles
    why: str | Callable[[dict], str]                 # the default sentence (also the model's example)
    variants: tuple[tuple[Callable[[dict], bool], str], ...] = ()      # (when, sentence) tried first: a zero figure that would read badly
    later: Callable[[dict], str] | None = None       # the sentence when this move is for a LATER waterfall step ("Once {once}, ...")


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
        "You have {starter_cushion_now} in cash against a starter fund of {starter_cushion_target}; adding {starter_cushion_gap} gets you there.",
        ((lambda f: f["liquid"] < ZERO, "You have no cash set aside yet; building a starter fund of {starter_cushion_target} is the first goal."),
         (lambda f: "retirement_saving_monthly" in f, "You have {starter_cushion_now} in cash against a starter fund of {starter_cushion_target}; adding {starter_cushion_gap} would take about {starter_target_months}, since about {cash_build_monthly} a month builds up in cash after your {retirement_saving_monthly} a month of retirement saving."),
         (lambda f: "months_to_target" in f, "You have {starter_cushion_now} in cash against a starter fund of {starter_cushion_target}; adding {starter_cushion_gap} would take about {starter_target_months} at the {cash_build_monthly} a month that builds up in cash."))),
    "confirm_401k_match": Spec(
        "The customer's employer offers a 401(k) match, and we cannot see whether they get all of it.",
        (P("match_pct", lambda f: f["employer_match_pct"], pct, "share of pay the employer matches"),
         P("match_monthly", lambda f: f["monthly_to_get_match"], usd, "what the full match is worth each month")),
        "Confirm your 401(k) match",
        "Your employer matches up to {match_pct} of your pay, worth about {match_monthly} a month, so check that you contribute enough to get all of it.",
        later=lambda f: "Once {once}, check that you contribute enough to get your employer's match of {match_pct} of your pay, worth about {match_monthly} a month."),
    "pay_card_high_apr": Spec(
        "A credit card charges high interest, more than savings can earn.",
        (P("card_balance", lambda f: f["total_balance"], usd, "balance owed on high-interest cards"),
         P("card_apr", lambda f: f["highest_apr"], pct, "the highest yearly interest rate")),
        "Pay down your high-interest card",
        "Your card balance of {card_balance} costs {card_apr} a year in interest, so extra money goes here before other goals.",
        later=lambda f: "Once {once}, put extra money toward your card balance of {card_balance}, which costs {card_apr} a year in interest."),
    "pay_loan_high_apr": Spec(
        "A loan charges high interest, more than savings can earn.",
        (P("loan_balance", lambda f: f["total_balance"], usd, "balance owed on high-interest loans"),
         P("loan_apr", lambda f: f["highest_apr"], pct, "the highest yearly interest rate")),
        "Pay down your high-interest loan",
        "Your loan balance of {loan_balance} costs {loan_apr} a year in interest, so extra money goes here before other goals.",
        later=lambda f: "Once {once}, put extra money toward your loan balance of {loan_balance}, which costs {loan_apr} a year in interest."),
    "build_full_cushion": Spec(
        "Cash on hand is below a full emergency cushion.",
        (P("cushion_now", lambda f: f["liquid"], usd, "cash on hand today"),
         P("cushion_target", lambda f: f["target"], usd, "size of the full cushion"),
         P("cushion_gap", lambda f: f["gap"], usd, "how much more is needed"),
         P("cushion_months", lambda f: f["months"], months, "how many months of essentials the cushion covers, with the unit")),
        "Build your full emergency cushion",
        "You have {cushion_now} in cash against a full cushion of {cushion_target} ({cushion_months} of essentials), so {cushion_gap} more is needed.",
        ((lambda f: f["liquid"] < ZERO, "You have no cash set aside yet; a full cushion of {cushion_target} ({cushion_months} of essentials) is the goal."),
         (lambda f: "retirement_saving_monthly" in f, "You have {cushion_now} in cash against a full cushion of {cushion_target} ({cushion_months} of essentials); {cushion_gap} more would take about {cushion_target_months}, since about {cash_build_monthly} a month builds up in cash after your {retirement_saving_monthly} a month of retirement saving."),
         (lambda f: "months_to_target" in f, "You have {cushion_now} in cash against a full cushion of {cushion_target} ({cushion_months} of essentials); {cushion_gap} more would take about {cushion_target_months} at the {cash_build_monthly} a month that builds up in cash.")),
        lambda f: ("Once {once}, start building a full cushion of {cushion_target} ({cushion_months} of essentials)." if f["liquid"] < ZERO else
                   "Once {once}, build your cash from {cushion_now} up to a full cushion of {cushion_target} ({cushion_months} of essentials).")),
    "increase_retirement": Spec(
        "Retirement saving is below the usual target share of pay.",
        (P("retirement_monthly_now", lambda f: f["current_monthly"], usd, "retirement saving each month today"),
         P("retirement_monthly_target", lambda f: f["target_monthly"], usd, "the target each month"),
         P("retirement_monthly_gap", lambda f: f["gap_monthly"], usd, "how much more each month")),
        "Save more for retirement",
        "You save about {retirement_monthly_now} a month toward retirement against a target of {retirement_monthly_target}, so {retirement_monthly_gap} more a month closes the gap.",
        ((lambda f: f["current_monthly"] < ZERO, "You aren't saving for retirement yet; aiming for about {retirement_monthly_target} a month would get you to the target."),),
        lambda f: ("Once {once}, start saving for retirement: aim for about {retirement_monthly_target} a month." if f["current_monthly"] < ZERO else
                   "Once {once}, raise your retirement saving from about {retirement_monthly_now} a month toward {retirement_monthly_target}.")),
    "start_education_savings": Spec(
        "The customer has children under 18 and the earlier goals are under way. Retirement saving comes first.",
        (),
        "Start saving for education",
        "Retirement saving comes first; once that is covered, start setting money aside for your children's education.",
        later=lambda f: "Once {once}, start setting money aside for your children's education; retirement saving comes first."),
    "start_credit_building": Spec(
        "The credit score is low and there is no open credit card.",
        (P("credit_score", lambda f: f["credit_score"], plain, "the current credit score"),),
        "Start building your credit",
        "With a credit score of {credit_score}, one card used lightly and paid in full each month can help build it.",
        ((lambda f: f.get("secured_card", False), "With a credit score of {credit_score}, a secured card (backed by a deposit you put down) used lightly and paid in full each month can help build it."),)),
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
        "About {monthly_surplus} a month is left over and only {automated_monthly} is moved to savings automatically; automating more makes saving happen first.",
        ((lambda f: f["automated_monthly"] < ZERO, "About {monthly_surplus} a month is left over and nothing is moved to savings automatically; automating it makes saving happen first."),)),
}

# Which placeholders are percentages / counts / dates is carried by the name (see above); the grounding facts use these exact names.
_COUNT_UNIT = re.compile(r"\b(months?)\s+months?\b", re.I)


def tidy(text: str) -> str:
    """fill() fixes doubled % and $; the placeholders for lengths of time carry their unit too ("6 months"), so fix "6 months months"."""
    return _COUNT_UNIT.sub(r"\1", text)


# placeholders that exist only when the facts carry them: (move codes or None for any, when, placeholder)
OPTIONAL: tuple[tuple[tuple[str, ...] | None, Callable[[dict], bool], P], ...] = (
    (("build_full_cushion",), lambda f: "months_to_target" in f,
     P("cushion_target_months", lambda f: f["months_to_target"], months, "how long the gap takes if all the monthly surplus goes to it, with the unit")),
    (("build_starter_cushion",), lambda f: "months_to_target" in f,
     P("starter_target_months", lambda f: f["months_to_target"], months, "how long the gap takes if all the monthly surplus goes to it, with the unit")),
    (("build_full_cushion", "build_starter_cushion"), lambda f: "cash_build_monthly" in f,
     P("cash_build_monthly", lambda f: f["cash_build_monthly"], usd, "the cash that really builds up each month, after retirement saving")),
    (("build_full_cushion", "build_starter_cushion"), lambda f: "retirement_saving_monthly" in f,
     P("retirement_saving_monthly", lambda f: f["retirement_saving_monthly"], usd, "what already goes to a retirement account each month")),
    (None, lambda f: "freed_amount" in f, P("freed_amount", lambda f: f["freed_amount"], usd, "the monthly payment that frees up when a loan payment ends")),
    (None, lambda f: "freed_amount" in f, P("freed_date", lambda f: f["freed_date"], day, "the date that loan payment ended or ends")),
)

# what "Once ..." completes, by the step the customer is on now (after_step); "gap" = spending is above income
ONCE = {"gap": "your monthly spending is covered", 1: "your starter cushion is in place", 2: "you have your full employer match",
        3: "your high-interest debt is paid off", 4: "your emergency cushion is full", 5: "your retirement saving reaches its target",
        6: "education saving is under way"}


@dataclass
class MoveText:
    code: str
    ph: dict[str, str]                 # name -> text the customer reads
    values: dict[str, Any]             # name -> raw value, for the grounding check
    means: dict[str, str]


def move_text(code: str, facts: dict) -> MoveText:
    spec = SPECS[code]
    ph, values = {}, {}
    extra = tuple(p for codes, when, p in OPTIONAL if (codes is None or code in codes) and when(facts))
    for p in (*spec.ph, *extra):
        raw = p.get(facts)
        ph[p.name], values[p.name] = p.fmt(raw), raw
    return MoveText(code, ph, values, {p.name: p.means for p in (*spec.ph, *extra)})


def sentence(code: str, facts: dict) -> str:
    """The move's sentence with placeholders still in it, chosen in code: a zero-figure variant when one applies, else the default."""
    spec = SPECS[code]
    if "after_step" in facts and spec.later is not None:               # a move for a later step: "Once your cushion is full, ..."
        text = spec.later(facts).replace("{once}", ONCE[facts["after_step"]])
    else:
        text = next((t for when, t in spec.variants if when(facts)), None) or (spec.why(facts) if callable(spec.why) else spec.why)
    if "freed_amount" in facts:                                          # a freed payment folded into the pinned move: its own, second sentence
        text = text.rstrip(".") + ". " + freed_sentence(code, facts)
    return text


def freed_sentence(code: str, facts: dict) -> str:
    """The second sentence of the pinned move when a loan payment has freed up (or is about to). A loan that ended BEFORE today is already
    in the surplus: the sentence says the money includes it. One that ends later is not yet: the sentence says what happens then."""
    cushion = code in ("build_full_cushion", "build_starter_cushion")
    if facts.get("freed_state") == "ended":
        return ("That includes the {freed_amount} a month freed up when your loan ended on {freed_date}." if cushion else
                "The extra money you have includes the {freed_amount} a month freed up when your loan ended on {freed_date}.")
    return ("When your loan payment ends on {freed_date}, the {freed_amount} a month it frees up can go straight to it." if cushion else
            "When your loan is paid off on {freed_date}, its {freed_amount} a month is free for your next goal.")


def template_move(code: str, facts: dict) -> tuple[str, str]:
    """The deterministic title and sentence for a move: the catalogue text with its own placeholders filled in."""
    mt = move_text(code, facts)
    return SPECS[code].title, tidy(grounding.fill(sentence(code, facts), mt.ph))


# ---- inputs --------------------------------------------------------------------------------------------------------------------

@dataclass
class Inputs:
    """What the formulas decided, before any model is involved."""
    waterfall: rules.Waterfall
    moves: rules.CandidateMoves
    verdict: str
    candidates: dict[str, rules.CandidateMove]       # unique by move_code, in waterfall order (the first of each code wins)
    labels: dict[str, str]                           # the forecast labels, for the wording checks
    folded: list[str] = field(default_factory=list)  # candidates merged into the pinned move instead of ranked separately


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
    inp = Inputs(wf, moves, v, candidates, labels)
    _fold_freed_payment(inp)
    return inp


def _fold_freed_payment(inp: Inputs) -> None:
    """On step 3 or 4, a payment that has just freed up (or is about to) belongs in the sentence about the current step, where it can
    speed that step up, not as a separate move that competes with it. Only when its monthly amount is known."""
    freed, pinned = inp.candidates.get("recently_freed_payment"), pinned_code(inp)
    if freed is None or pinned is None or inp.waterfall.current_step not in (3, 4):
        return
    known = [e for e in freed.facts["freed"] if e.get("monthly_amount")]
    if not known:
        return
    inp.candidates[pinned] = inp.candidates[pinned].model_copy(update={"facts": {
        **inp.candidates[pinned].facts, "freed_amount": round(sum(e["monthly_amount"] for e in known), 2),
        "freed_date": known[0]["ends"], "freed_state": known[0]["state"], "freed_in_surplus": known[0]["state"] == "ended"}})
    del inp.candidates["recently_freed_payment"]
    inp.folded.append("recently_freed_payment")


# ---- the model's view ----------------------------------------------------------------------------------------------------------

def model_view(inp: Inputs, facts: dict) -> str:
    """Plain language, the codes the model must name, and placeholders. No figures, no status codes."""
    lines = [
        f"Customer: {'retired' if facts.get('employment') == 'retired' else 'employed'}"
        f"{', often runs low before payday' if facts.get('paycheck_to_paycheck') == 'yes' else ''}.",
        f"Cash outlook: {TREND_TEXT.get(facts.get('trend'), 'unclear')}; it {CUSHION_TEXT.get(facts.get('cushion_status'), '')}; "
        f"{OVERDRAFT_TEXT.get(facts.get('overdraft_status'), '')}.",
        f"Urgency set by the app (do not repeat it): {VERDICT_TEXT[inp.verdict]}."]
    pinned = pinned_code(inp)

    def entry(code: str, move) -> list[str]:
        spec, mt = SPECS[code], move_text(code, move.facts)
        return [f"move_code {code}: (situation, not to be copied) {spec.blurb(move.facts) if callable(spec.blurb) else spec.blurb}",
                "   placeholders: " + ("; ".join(f"{{{k}}} = {v}" for k, v in mt.means.items()) or "none (write no figures)"),
                f"   example sentence (adapt this one, keep its braces): {sentence(code, move.facts)}"]

    if pinned:
        lines += ["The app's first move (write its sentence as pinned_why, adapting its example sentence):"] + entry(pinned, inp.candidates[pinned])
    rest = [c for c in inp.candidates if c != pinned]
    if rest:
        lines.append(f"Other moves (choose up to {MAX_MOVES - bool(pinned)} for \"more\", in the app's suggested order):")
        for i, code in enumerate(rest, 1):
            first, *others = entry(code, inp.candidates[code])
            lines += [f"{i}. {first}", *others]
    return "\n".join(lines)


# ---- checking ------------------------------------------------------------------------------------------------------------------

@dataclass
class Checked:
    move: AdvisorMove                  # with the placeholders filled in
    report: grounding.GroundingReport
    no_figures: bool = False           # the sentence should show a figure and uses none of the move's placeholders
    zero_figure: bool = False          # the filled sentence states a zero amount ("$0")
    missing: list[str] = field(default_factory=list)   # required wording absent: missing_once, missing_secured_card, freed_not_mentioned

    @property
    def ok(self) -> bool:
        return self.report.ok and not self.no_figures and not self.zero_figure and not self.missing

    @property
    def reasons(self) -> list[str]:
        """Why it failed, as codes only (never text): figure_not_in_facts, judgment, style, contradicts_<label>, no_placeholder, zero_figure."""
        out = ["figure_not_in_facts"] if self.report.unmatched else []
        out += [c.field if c.field in ("judgment", "style") else f"contradicts_{c.field}" for c in self.report.conflicts]
        out += ["no_placeholder"] * self.no_figures + ["zero_figure"] * self.zero_figure + self.missing
        return list(dict.fromkeys(out))


def _fill_all(inp: Inputs) -> dict[str, str]:
    out: dict[str, str] = {}
    for code, move in inp.candidates.items():
        out |= move_text(code, move.facts).ph
    return out


def sentence_count(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip()) if s])


def check_move(m: AdvisorMove, inp: Inputs, pinned: bool = False) -> Checked:
    """Fill the placeholders, then check the words against ONLY this move's own facts (a figure from another move is not allowed)."""
    move = inp.candidates[m.move_code]
    mt = move_text(m.move_code, move.facts)
    filled = AdvisorMove(move_code=m.move_code, why_text=tidy(grounding.fill(m.why_text, _fill_all(inp)) or ""))
    facts = {**mt.values, **inp.labels}
    report = grounding.check(filled.why_text, facts, judgments=True, style=True, hints=False)
    wants_figures = "{" in sentence(m.move_code, move.facts)                       # a zero variant may have none to show
    unused = wants_figures and not any("{" + k + "}" in m.why_text for k in mt.ph)
    missing = [reason for reason, ok in (
        ("missing_once", "after_step" not in move.facts or bool(re.search(r"\bonce\b", filled.why_text, re.I))),
        ("missing_secured_card", not move.facts.get("secured_card") or "secured card" in filled.why_text.lower()),
        ("freed_not_mentioned", "freed_amount" not in move.facts or "{freed_amount}" in m.why_text),
        # one sentence per move; the pinned move may use two (the gap and its timeline, then the freed payment)
        ("too_long", sentence_count(filled.why_text) <= (2 if pinned else 1))) if not ok]
    return Checked(filled, report, unused, bool(ZERO_FIGURE.search(filled.why_text)), missing)


def pinned_code(inp: Inputs) -> str | None:
    """The move for the waterfall's current step. Code puts it first: the model decides its wording and what follows, not whether the
    most pressing step is mentioned. (The first such move when several serve the step.)"""
    return next((code for code, m in inp.candidates.items() if m.addresses_current_step), None)


def required(inp: Inputs) -> list[str]:
    return [c] if (c := pinned_code(inp)) else []


def _complaint(invalid: list[str], bad: list[Checked], inp: Inputs, missing: list[str]) -> str:
    parts = []
    if missing:
        parts.append(f"You must write pinned_why: the sentence for {', '.join(missing)}.")
    if invalid:
        parts.append(f"These move codes are not in the list of other moves: {', '.join(invalid)}. "
                     f"Use only: {', '.join(c for c in inp.candidates if c != pinned_code(inp))}.")
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
        if c.zero_figure:
            bits.append("it states a zero amount; use the wording of the example sentence")
        if c.missing:
            bits.append("it must keep the wording of the example sentence for: " + ", ".join(x.replace("_", " ") for x in c.missing))
        if any(x.field == "style" for x in r.conflicts):
            bits.append("not plain English; use only the listed {placeholders}: " + "; ".join(f'"{x.phrase}"' for x in r.conflicts if x.field == "style"))
        parts.append(f"For {c.move.move_code}: " + "; ".join(bits) + ".")
    return " ".join(parts) + " Rewrite the answer, fixing only these problems, and keep the JSON format."


def _screen(answer: AdvisorAnswer, inp: Inputs) -> tuple[list[Checked], list[str], list[Checked], list[str]]:
    """(the checked moves, pinned one first, then the model's others in its order up to MAX_MOVES; unknown codes; those that failed
    the checks; the pinned move if the model wrote no sentence for it)."""
    pinned, good, invalid, missing = pinned_code(inp), [], [], []
    seen = {pinned}
    if pinned:
        if (answer.pinned_why or "").strip():
            good.append(check_move(AdvisorMove(move_code=pinned, why_text=answer.pinned_why), inp, pinned=True))
        else:
            missing.append(pinned)
    for m in answer.more:
        if m.move_code not in inp.candidates:
            invalid.append(m.move_code[:40])
        elif m.move_code not in seen and len(good) + len(missing) < MAX_MOVES:
            seen.add(m.move_code)
            good.append(check_move(m, inp))
    return good, invalid, [c for c in good if not c.ok], missing


def _reasons(invalid: list[str], bad: list[Checked], missing: list[str], empty: bool) -> list[str]:
    out = ["unknown_move_code"] * bool(invalid) + [r for c in bad for r in c.reasons] + ["missing_required_move"] * bool(missing)
    out += ["no_moves"] * (empty and not invalid)
    return list(dict.fromkeys(out))


# ---- the run -------------------------------------------------------------------------------------------------------------------

@dataclass
class _Tally:
    checked: int = 0
    retried: bool = False
    first_ok: bool = False
    first_reasons: list[str] = field(default_factory=list)
    final_reasons: list[str] = field(default_factory=list)
    calls: list[client.LLMResult] = field(default_factory=list)
    bad_replies: list[BadReply] = field(default_factory=list)


def _note_bad_reply(tally: "_Tally", e: client.LLMResponseError) -> None:
    tally.bad_replies += [BadReply(finish_reason=a["finish_reason"], completion_tokens=a["completion_tokens"], max_tokens=e.max_tokens)
                          for a in e.attempts]


def _ordered(moves: list[AdviceMove]) -> list[AdviceMove]:
    """Retirement before education, always (docs/rules.md section 1)."""
    codes = [m.move_code for m in moves]
    if "increase_retirement" in codes and "start_education_savings" in codes and codes.index("start_education_savings") < codes.index("increase_retirement"):
        i, j = codes.index("start_education_savings"), codes.index("increase_retirement")
        moves[i], moves[j] = moves[j], moves[i]
    return moves


def _assemble(inp: Inputs, picked: list[tuple[str, str]], tally: _Tally) -> Advice:
    """picked: (move_code, why_text, source); the title always comes from the catalogue."""
    moves = _ordered([AdviceMove(rank=0, move_code=c, title_text=SPECS[c].title, why_text=w, source=s, priority=inp.candidates[c].priority,
                                 addresses_current_step=inp.candidates[c].addresses_current_step) for c, w, s in picked])
    for i, m in enumerate(moves, 1):
        m.rank = i
    sources = {m.source for m in moves}
    notes = []
    if "increase_retirement" in inp.candidates and "start_education_savings" in inp.candidates:
        notes.append("Retirement saving comes before education saving.")
    return Advice(verdict=inp.verdict, current_step=inp.waterfall.current_step, moves=moves, notes=notes,
                  source="model" if sources == {"model"} else "template" if sources <= {"template"} else "mixed",
                  grounding=GroundingSummary(ok=True, checked=tally.checked, retried=tally.retried),
                  diagnostics=AdviceDiagnostics(first_try_ok=tally.first_ok, retried=tally.retried, model_calls=len(tally.calls),
                                                template_moves=sum(m.source == "template" for m in moves),
                                                first_reasons=tally.first_reasons, final_reasons=tally.final_reasons,
                                                bad_replies=tally.bad_replies))


def template_advice(inp: Inputs, tally: _Tally | None = None, reason: str | None = None) -> Advice:
    """The model's absence: the top candidates in waterfall order with template text. `reason`: why the model's answer was not used
    (llm_unavailable, llm_bad_reply, budget_exceeded...)."""
    tally = tally or _Tally()
    if reason:
        tally.first_reasons = tally.first_reasons or [reason]
        tally.final_reasons = tally.final_reasons or [reason]
    pinned = pinned_code(inp)                                         # the current step's move first, then waterfall order
    codes = ([pinned] if pinned else []) + [c for c in inp.candidates if c != pinned]
    picked = [(code, template_move(code, inp.candidates[code].facts)[1], "template") for code in codes[:MAX_MOVES]]
    return _assemble(inp, picked, tally)


async def advise(inp: Inputs, facts: dict, budget_s: float | None = None) -> Advice:
    """The model's choice, checked; the template wherever the model's words fail. Never raises because the model failed or was slow:
    past `budget_s` (default: config advisor_budget_s; math.inf = no limit) the template result is served."""
    limit = llm_config.advisor_budget_s() if budget_s is None else budget_s
    tally = _Tally()
    try:
        async with asyncio.timeout(None if limit == math.inf else limit):
            return await _advise(inp, facts, tally)
    except TimeoutError:
        return template_advice(inp, _Tally(retried=tally.retried, calls=tally.calls), "budget_exceeded")


async def _advise(inp: Inputs, facts: dict, tally: _Tally) -> Advice:
    if not inp.candidates:
        tally.first_ok = True
        return _assemble(inp, [], tally)
    if list(inp.candidates) == [pinned_code(inp)]:                 # nothing to choose and the code already wrote the order: no model call
        tally.first_ok = True
        return template_advice(inp, tally, "single_move")
    messages = [{"role": "system", "content": ADVISOR_PROMPT}, {"role": "user", "content": model_view(inp, facts)}]
    ask = lambda msgs, **kw: client.complete("advisor", msgs, json_schema=AdvisorAnswer, max_tokens=ADVISOR_MAX_TOKENS, schema_in_prompt=False,
                                             response_schema=response_schema(inp), **kw)
    try:
        first = await ask(messages)
    except client.LLMUnavailable:
        return template_advice(inp, tally, "llm_unavailable")
    except client.LLMError as e:
        if isinstance(e, client.LLMResponseError):
            _note_bad_reply(tally, e)
        return template_advice(inp, tally, "llm_bad_reply")
    tally.calls.append(first)
    good, invalid, bad, missing = _screen(first.parsed, inp)
    tally.first_ok = not (invalid or bad or not good or missing)
    tally.first_reasons = _reasons(invalid, bad, missing, not good and not missing)
    if not tally.first_ok:
        tally.retried = True
        complaint = _complaint(invalid, bad, inp, missing) if (invalid or bad or good or missing) else (
            f"You chose no move. Choose up to {MAX_MOVES} from: {', '.join(inp.candidates)}.")
        try:
            answer = await ask(messages + [{"role": "assistant", "content": first.content}, {"role": "user", "content": complaint}],
                               temperature=grounding.RETRY_TEMPERATURE)
            tally.calls.append(answer)
            good, invalid, bad, missing = _screen(answer.parsed, inp)
            tally.final_reasons = _reasons(invalid, bad, missing, not good and not missing)
        except client.LLMError as e:
            if isinstance(e, client.LLMResponseError):
                _note_bad_reply(tally, e)
            good, bad, missing = [], [], required(inp)
            tally.final_reasons = ["llm_unavailable" if isinstance(e, client.LLMUnavailable) else "llm_bad_reply"]
    tally.checked = sum(len(c.report.numbers) for c in good)
    if not good:                                                   # nothing usable from the model: top candidates, template text
        return template_advice(inp, tally)
    failed = {c.move.move_code for c in bad}
    picked = [(c.move.move_code, c.move.why_text, "model") if c.move.move_code not in failed else
              (c.move.move_code, template_move(c.move.move_code, inp.candidates[c.move.move_code].facts)[1], "template") for c in good]
    for code in reversed(missing):                                 # no sentence for the pinned move after the retry: the template, first
        picked.insert(0, (code, template_move(code, inp.candidates[code].facts)[1], "template"))
    return _assemble(inp, picked[:MAX_MOVES], tally)
