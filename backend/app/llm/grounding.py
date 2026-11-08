"""Grounding check: every figure in a model-written text must come from the facts the model was given.

check(text, facts) pulls out the numeric claims (money, percentages, dates, counts with units) and matches each against the numbers in
the facts (flattened with their paths) plus a few explicit derived forms. A figure that exists under a different fact fails too: the
words next to it ("overdraft", "cushion", "a month", "lowest") must agree with the fact's path.

grounded_complete(...) wraps complete(): check, retry once naming the bad figures, else fall back to template(facts), which is built
only from the facts. The outcome goes into the run trace, so the UI can show "All figures verified".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from . import client

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_MONTH_RE = "|".join(m.capitalize() for m in MONTHS) + "|" + "|".join(m[:3].capitalize() for m in MONTHS)
WORD_NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())} | {
    "eighteen": 18, "twenty-four": 24, "thirty-six": 36}

MONEY = re.compile(r"(?P<q>(?:\b(?:about|around|roughly|approximately|approx\.?|nearly|almost|close to)\s+|~\s*)?)"
                   r"\$\s?(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<k>[kK]\b)?")
PERCENT = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s?(?:%|per ?cent\b)")
COUNT = re.compile(r"\b(?P<n>\d+|" + "|".join(WORD_NUMBERS) + r")[ -](?P<u>months?|days?|weeks?|years?)\b", re.I)
DATE_DAY = re.compile(rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<y>\d{{4}}))?")
DATE_MONTH = re.compile(rf"\b(?:(?:early|mid|late)[- ])?(?P<m>{_MONTH_RE})\b(?!\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b)(?:,?\s+(?P<y>\d{{4}}))?")
# a number with no $ or unit: "10342.27", "1,629". Years and small whole numbers ("2 sentences") are not claims.
BARE = re.compile(r"(?<![\w$.\-/,])(?P<n>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w%/]|[.,]\d|-\d)")
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# Words next to a figure -> substrings the matching fact's path must contain. Only applied when some fact path contains one.
KEYWORDS: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"overdraft|overdraw|negative", re.I), ("overdraft",)),
    (re.compile(r"cushion|safety|buffer", re.I), ("cushion",)),
    (re.compile(r"\ba month\b|per month|each month|monthly|/mo\b", re.I), ("monthly",)),
    (re.compile(r"\bgain|\bgrow|\bsav(?:e|ing)", re.I), ("gain", "monthly", "end_balance", "balance_in_12_months")),
    (re.compile(r"\blow(?:est)?\b|\bdips?\b|\bdrops?\b|bottom", re.I), ("low_point",)),
    (re.compile(r"\bend\b|ending|final", re.I), ("end_balance", "balance_in_12_months")),
    (re.compile(r"\bstart|currently|today|\bnow\b", re.I), ("start_balance",)),
    (re.compile(r"\braise|renewal|\brent\b|\bloan\b", re.I), ("next_events",)),
]


@dataclass(frozen=True)
class Fact:
    path: str
    value: float | str            # a number, or an ISO date string
    kind: str                     # money | ratio | count | date


@dataclass
class CheckedNumber:
    text: str
    value: float | str
    kind: str
    matched_fact: str | None


@dataclass
class Conflict:
    phrase: str          # the wording in the text, e.g. "large cushion"
    field: str           # the status label it contradicts, e.g. "cushion_status"
    status: str          # ...and that label's value, e.g. "dips_temporarily"


@dataclass
class GroundingReport:
    ok: bool                                           # every figure matched a fact and no wording contradicts a status label
    numbers: list[CheckedNumber] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def unmatched(self) -> list[CheckedNumber]:
        return [n for n in self.numbers if n.matched_fact is None]


# ---- the allowed values --------------------------------------------------------------------------------------------------------

def _kind_of(path: str) -> str:
    leaf = path.rsplit(".", 1)[-1].split("[")[0]
    if leaf.startswith(("share_of", "chance_of", "probability")):
        return "ratio"
    return "count" if leaf in ("horizon_months",) else "money"


def flatten(facts: Any, path: str = "") -> list[Fact]:
    """Every number, date and figure-bearing string in the facts, with its path ('low_point.amount', 'next_events[0].monthly_impact')."""
    out: list[Fact] = []
    if isinstance(facts, dict):
        for k, v in facts.items():
            out += flatten(v, f"{path}.{k}" if path else k)
    elif isinstance(facts, list):
        for i, v in enumerate(facts):
            out += flatten(v, f"{path}[{i}]")
    elif isinstance(facts, bool) or facts is None:
        pass
    elif isinstance(facts, (int, float)):
        out.append(Fact(path, float(facts), _kind_of(path)))
    elif isinstance(facts, str):
        if DATE_ISO.fullmatch(facts):
            out.append(Fact(path, facts, "date"))
        else:                                                     # a description such as "Annual raise of 3.1%": its figures are facts too
            out += [Fact(path, float(m["n"]) / 100, "ratio") for m in PERCENT.finditer(facts)]
            out += [Fact(path, float(m["n"].replace(",", "")), "money") for m in MONEY.finditer(facts)]
    return out


def derived(facts: dict) -> list[Fact]:
    """The few explicit derivations: annualised monthly figures and differences between two facts. Nothing else is allowed."""
    get = lambda *keys: _dig(facts, keys)
    out: list[Fact] = []
    start, gain, low, cushion = get("start_balance"), get("monthly_gain"), get("low_point", "amount"), get("cushion")
    add = lambda path, v: out.append(Fact(path, abs(v), "money")) if v is not None else None
    if gain is not None:
        add("monthly_gain*12", gain * 12)
    for i, e in enumerate(facts.get("next_events", [])):
        if e.get("monthly_impact") is not None:
            add(f"next_events[{i}].monthly_impact*12", e["monthly_impact"] * 12)
    for name, end in (("end_balance", get("end_balance", "p50")), ("balance_in_12_months", get("balance_in_12_months", "p50"))):
        if end is not None and start is not None:
            add(f"gain_over_horizon({name}.p50-start_balance)", end - start)
    if start is not None and low is not None:
        add("drop_to_low_point(start_balance-low_point.amount)", start - low)
    if cushion is not None and low is not None:
        add("cushion_gap(cushion-low_point.amount)", cushion - low)
    return out


def _dig(d: Any, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d if isinstance(d, (int, float)) and not isinstance(d, bool) else None


# ---- matching ------------------------------------------------------------------------------------------------------------------

def _money_matches(v: float, approx: bool, step: float | None, fact: float) -> bool:
    if abs(v - fact) <= 0.5 + 1e-9:                                # to the cent, or the nearest dollar
        return True
    if not approx:
        return False
    if abs(v - fact) <= 0.02 * abs(fact) + 1e-9:                   # "about": within 2% ...
        return True
    # ... or the fact rounded the way it was written ($10k, $1,600), unless that is more than 5% off
    return step is not None and abs(v - round(fact / step) * step) < 1e-6 and abs(v - fact) <= 0.05 * abs(fact)


def _step(num: str, k: bool) -> float | None:
    """How coarsely a figure was written: $11k -> 1000, $4.3k -> 100, $1,600 -> 100, $550 -> 10. None for a precise figure."""
    if k:
        decimals = len(num.split(".")[1]) if "." in num else 0
        return 1000 / 10 ** decimals
    if "." in num:
        return None
    n = int(num.replace(",", ""))
    for step in (1000, 100, 10):
        if n and n % step == 0:
            return float(step)
    return None


def _hints(text: str, start: int, end: int) -> tuple[str, ...] | None:
    """Path substrings the words around a figure ask for: its own clause, up to 30 characters before and 15 after."""
    before = re.split(r"[.;,:()]", text[max(0, start - 30):start])[-1]
    after = re.split(r"[.;,:()]", text[end:end + 15])[0]
    hints: list[str] = []
    for pat, subs in KEYWORDS:
        if pat.search(before) or pat.search(after):
            hints += subs
    return tuple(hints) or None


def _date_parts(iso: str) -> tuple[int, int, int]:
    d = date.fromisoformat(iso)
    return d.year, d.month, d.day


def check(text: str, facts: dict, context_numbers: tuple[float, ...] = (), judgments: bool = False) -> GroundingReport:
    """context_numbers: figures that come from the question, not the facts (e.g. 12 when the horizon asked about is 12 months)."""
    allowed = flatten(facts) + derived(facts)
    dates = [f for f in allowed if f.kind == "date"]
    numbers: list[CheckedNumber] = []
    taken: list[tuple[int, int]] = []                          # spans already read, so "$5" is not also read as a count

    def claim(m: re.Match, kind: str) -> bool:
        if any(m.start() < e and s < m.end() for s, e in taken):
            return False
        taken.append((m.start(), m.end()))
        return True

    def paths_ok(pool: list[Fact], hints: tuple[str, ...] | None) -> list[Fact]:
        """Narrow the candidates to the facts the nearby words ask for, when the facts have such a topic at all (of any kind: a money
        figure next to "overdraft" must not match the cushion just because only the overdraft *chance* is a ratio)."""
        if hints and any(h in f.path for f in allowed for h in hints):
            return [f for f in pool if any(h in f.path for h in hints)]
        return pool

    for m in MONEY.finditer(text):
        if not claim(m, "money"):
            continue
        num = m["n"].replace(",", "")
        v = float(num) * (1000 if m["k"] else 1)
        pool = paths_ok([f for f in allowed if f.kind == "money"], _hints(text, m.start(), m.end()))
        approx = bool(m["q"].strip()) or bool(m["k"])             # "about", "~" and "$4.3k" are rounded figures; a bare "$1,600" is exact
        step = _step(num, bool(m["k"])) if approx else None
        hit = next((f for f in pool if _money_matches(v, approx, step, abs(f.value))), None)
        numbers.append(CheckedNumber(m.group(0).strip(), v, "money", hit.path if hit else None))

    for m in PERCENT.finditer(text):
        if not claim(m, "ratio"):
            continue
        v = float(m["n"])
        pool = paths_ok([f for f in allowed if f.kind == "ratio"], _hints(text, m.start(), m.end()))
        hit = next((f for f in pool if abs(v - 100 * f.value) <= 0.5 + 1e-9), None)
        numbers.append(CheckedNumber(m.group(0), v, "percent", hit.path if hit else None))

    for m in DATE_ISO.finditer(text):
        if claim(m, "date"):
            numbers.append(CheckedNumber(m.group(0), m.group(0), "date", next((f.path for f in dates if f.value == m.group(0)), None)))
    for m in DATE_DAY.finditer(text):
        if not claim(m, "date"):
            continue
        mon, day = MONTHS.index(_full_month(m["m"])) + 1, int(m["d"])
        hit = next((f for f in dates if (lambda y, mo, d: mo == mon and d == day and (not m["y"] or y == int(m["y"])))(*_date_parts(f.value))), None)
        numbers.append(CheckedNumber(m.group(0), f"{mon:02d}-{day:02d}", "date", hit.path if hit else None))
    for m in DATE_MONTH.finditer(text):
        word = m["m"]
        if not claim(m, "date") or (word == "May" and not m["y"] and not re.match(r"(early|mid|late)", m.group(0), re.I)):
            continue
        mon = MONTHS.index(_full_month(word)) + 1
        hit = next((f for f in dates if (lambda y, mo, d: mo == mon and (not m["y"] or y == int(m["y"])))(*_date_parts(f.value))), None)
        numbers.append(CheckedNumber(m.group(0), mon, "date", hit.path if hit else None))

    counts = [f for f in allowed if f.kind == "count"]
    for m in COUNT.finditer(text):
        if not claim(m, "count"):
            continue
        n = WORD_NUMBERS.get(m["n"].lower()) if not m["n"].isdigit() else int(m["n"])
        unit = m["u"].lower().rstrip("s")
        hit = next((f.path for f in counts if f.value == n and unit == "month"), None)
        if hit is None and n in context_numbers:
            hit = "context"
        numbers.append(CheckedNumber(m.group(0), n, "count", hit))

    for m in BARE.finditer(text):                              # last, so dates, money, percentages and counts have claimed theirs
        num = m["n"].replace(",", "")
        if "." not in num and (len(num) < 3 or 1900 <= int(num) <= 2100):
            continue
        if not claim(m, "bare"):
            continue
        v = float(num)
        pool = paths_ok([f for f in allowed if f.kind == "money"], _hints(text, m.start(), m.end()))
        hit = next((f for f in pool if _money_matches(v, False, None, abs(f.value))), None)
        numbers.append(CheckedNumber(m.group(0), v, "number", hit.path if hit else None))

    conflicts = check_wording(text, facts, judgments)
    return GroundingReport(all(n.matched_fact for n in numbers) and not conflicts, numbers, conflicts)


def _full_month(word: str) -> str:
    w = word.lower()
    return next(m for m in MONTHS if m.startswith(w[:3]))


# ---- wording vs the status labels ----------------------------------------------------------------------------------------------

_I = re.I
_PRAISE = r"(?:large|big|strong|healthy|comfortable|ample|solid|generous|substantial|good|plenty of)"
_UP = r"\b(?:grow\w*|ris(?:e|es|ing)|increas\w*|improv\w*|climb\w*)\b"
_DOWN = r"\b(?:shrink\w*|declin\w*|decreas\w*|deteriorat\w*|worsen\w*)\b"
_NEG_BEFORE = re.compile(r"\b(?:never|not|no|without|nor)\b|n't", _I)       # "never dips below the cushion" says the opposite
_LOW_AFTER = re.compile(r"\s*(?:is|are|looks?|seems?)\s+(?:very |extremely )?(?:minimal|low|negligible|zero|nil|none|unlikely|0(?:\.0+)?\s?(?:%|percent))", _I)


@dataclass(frozen=True)
class Rule:
    field: str                          # the status label
    values: tuple[str, ...]             # the label values the wording contradicts
    pattern: re.Pattern
    guard: bool = True                  # skip when a negation sits just before the phrase
    after: re.Pattern | None = None     # skip when this follows the phrase ("risk of overdraft is minimal")


RULES = [
    Rule("cushion_status", ("below_now", "dips_temporarily"),
         re.compile(rf"\b{_PRAISE}\s+(?:\w+\s+)?cushion|cushion\s+(?:is|looks|remains|stays)\s+(?:\w+\s+)?{_PRAISE}"
                    r"|\balways\s+(?:stays?\s+|remains?\s+|is\s+)?above|\bnever\s+(?:dips?|falls?|drops?|goes|go)\s+below"
                    r"|\bcomfortabl[ye]\s+above", _I)),
    Rule("trend", ("falling",), re.compile(_UP, _I)),
    Rule("trend", ("rising",), re.compile(_DOWN + r"|\blosing money", _I)),
    Rule("trend", ("flat",), re.compile(_UP + "|" + _DOWN, _I)),
    Rule("overdraft_status", ("none",),
         re.compile(r"\b(?:risk|chance|possibility|danger|likelihood)\s+of\s+(?:an?\s+)?overdraft\w*|\boverdraft\s+risk"
                    r"|\b(?:may|might|could)\s+overdraw", _I), after=_LOW_AFTER),
    Rule("overdraft_status", ("possible", "likely"),
         re.compile(r"\bno\s+(?:risk|chance|possibility)\s+of\s+(?:an?\s+)?overdraft|\bno\s+overdraft|\bnever\s+overdraw", _I), guard=False),
    Rule("overdraft_status", ("likely",), re.compile(r"\bunlikely\b|\b(?:minimal|low|small)\s+(?:risk|chance)", _I), guard=False),
    Rule("cushion_status", ("stays_above",),
         re.compile(r"\bshort(?:fall)?\b(?![- ]term)|\b(?:dips?|dipping|falls?|drops?|goes|go)\s+(?:temporarily\s+)?below\s+(?:the|your)\s+cushion"
                    r"|\bbelow\s+(?:the|your)\s+cushion", _I)),
]


# Judgments belong to the app (its overall_status badge), not to the prose. Only enforced for explanation-style answers.
EVALUATIVE = re.compile(
    r"\b(?:no|zero|little|minimal|low|high|significant|major|serious|real|negligible|substantial)\s+(?:\w+\s+)?risks?\b|\brisk[- ]free\b"
    r"|\bsafe(?:ly)?\b|\bsecure\b|\bhealthy\b|\b(?:on|off) track\b|\bgood shape\b|\bwell[- ]positioned\b|\bnothing to worry\b"
    r"|\bno (?:cause|need|reason) (?:for|to) (?:concern|worry|alarm)\b|\bworr\w+|\bconcern(?:ing|ed)?\b|\breassur\w+|\balarm\w*"
    r"|\bcomfortabl[ye]\b|\bdanger\w*|\bcritical\b|\bin trouble\b|\bdoing (?:well|great|fine)\b", re.I)


def check_wording(text: str, facts: dict, judgments: bool = False) -> list[Conflict]:
    """Phrases in the text that contradict the facts' status labels (cushion_status, trend, overdraft_status). With judgments=True,
    also evaluative wording ("no significant risk", "safe", "on track"): the app states the overall judgment, not the prose."""
    out: list[Conflict] = [Conflict(m.group(0), "judgment", facts.get("overall_status", "set by the app"))
                           for m in EVALUATIVE.finditer(text)] if judgments else []
    for rule in RULES:
        status = facts.get(rule.field)
        if status not in rule.values:
            continue
        for m in rule.pattern.finditer(text):
            if rule.guard and _NEG_BEFORE.search(text[max(0, m.start() - 15):m.start()]):
                continue
            if rule.after and rule.after.match(text, m.end()):
                continue
            out.append(Conflict(m.group(0), rule.field, status))
    return out


# ---- fallback text -------------------------------------------------------------------------------------------------------------

def _usd(x: float) -> str:
    return f"${abs(x):,.0f}"


def _day(iso: str, year: bool = False) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}" + (f", {d.year}" if year else "")


def template(facts: dict) -> str:
    """Deterministic text built from the facts alone, worded to match their status labels. It passes check()."""
    gain, low, cushion = facts["monthly_gain"], facts["low_point"], facts["cushion"]
    trend = facts.get("trend", "rising" if gain >= 0 else "falling")
    parts = [{"rising": f"Your balance is projected to grow about {_usd(gain)} a month.",
              "falling": f"Your balance is projected to shrink about {_usd(gain)} a month.",
              "flat": f"Your balance is roughly flat, changing about {_usd(gain)} a month."}[trend],
             f"The lowest point is about {_usd(low['amount'])} around {_day(low['date'])}."]
    status, until = facts.get("cushion_status"), facts.get("cushion_status_until")
    share = facts.get("share_of_days_below_cushion", 0)
    tail = (f", until {_day(until)}." if until else ".")
    if status == "stays_above" or (status is None and share == 0):
        parts.append(f"You stay above your cushion of about {_usd(cushion)}.")
    else:
        verb = "You are below" if status == "below_now" else "Your balance dips below"
        parts.append(f"{verb} your cushion of about {_usd(cushion)} on about {share * 100:.0f}% of days{tail}")
    chance = facts.get("chance_of_overdraft", 0)
    parts.append("No simulated path overdraws." if chance == 0 else f"About {chance * 100:.0f}% of simulated paths overdraw at some point.")
    end = facts.get("end_balance")
    if end:
        parts.append(f"By {_day(end['date'], year=True)} you would typically have about {_usd(end['p50'])}.")
    return " ".join(parts)


# ---- the loop ------------------------------------------------------------------------------------------------------------------

@dataclass
class GroundedResult:
    text: str                                # the main text field (the template's text when it was used)
    parsed: BaseModel                        # the whole answer: the model's, or the template wrapped in the schema
    report: GroundingReport
    retried: bool
    used_template: bool
    calls: list[client.LLMResult]            # every model call made (one, or two after a retry), for latency and token totals


def _texts(parsed: BaseModel, fields: tuple[str, ...]) -> str:
    return " ".join(v for f in fields if (v := getattr(parsed, f, None)))


async def grounded_complete(agent: str, messages: list[dict], facts: dict, schema: type[BaseModel], text_field: str = "summary",
                            extra_fields: tuple[str, ...] = (), context_numbers: tuple[float, ...] = (), forbid_judgments: bool = False,
                            **kwargs) -> GroundedResult:
    """complete() with json_schema=schema, then check `text_field` and `extra_fields` against `facts` (figures, wording, and with
    forbid_judgments, evaluative wording). Not grounded: one retry that names the problems, then template(facts) in `text_field`.
    Records the outcome in the run trace (counts and flags only)."""
    fields = (text_field, *extra_fields)
    verify = lambda parsed: check(_texts(parsed, fields), facts, context_numbers, forbid_judgments)
    result = await client.complete(agent, messages, json_schema=schema, **kwargs)
    calls, parsed = [result], result.parsed
    report = verify(parsed)
    retried = used_template = False
    if not report.ok:
        retried = True
        judged = [c for c in report.conflicts if c.field == "judgment"]
        complaint = ""
        if report.unmatched:
            complaint += f"These figures are not in the facts: {', '.join(n.text for n in report.unmatched)}. "
        if len(judged) < len(report.conflicts):
            complaint += ("These statements contradict the facts: "
                          + "; ".join(f'"{c.phrase}" ({c.field} is {c.status})' for c in report.conflicts if c.field != "judgment") + ". ")
        if judged:
            complaint += ("Do not state a judgment, risk level or reassurance; the app shows that separately. Remove: "
                          + "; ".join(f'"{c.phrase}"' for c in judged) + ". ")
        retry = await client.complete(
            agent, messages + [{"role": "assistant", "content": result.content},
                               {"role": "user", "content": complaint + "Rewrite using only figures from the facts."}],
            json_schema=schema, **kwargs)
        calls.append(retry)
        parsed = retry.parsed
        report = verify(parsed)
        if not report.ok:
            used_template, parsed = True, schema.model_validate({text_field: template(facts)})
            report = verify(parsed)
    client.record({"kind": "grounding", "agent": agent, "numbers_checked": len(report.numbers),
                   "wording_conflicts": len(report.conflicts), "retried": retried, "used_template": used_template, "ok": report.ok})
    return GroundedResult(getattr(parsed, text_field), parsed, report, retried, used_template, calls)


async def explain(agent: str, messages: list[dict], facts: dict, context_numbers: tuple[float, ...] = (), **kwargs) -> GroundedResult:
    """The standard writing step: an Explanation (explanation + optional next_step_hint) that is figure-checked, wording-checked and
    free of judgments. The overall judgment is facts["overall_status"], shown by the app."""
    from .schemas import Explanation
    return await grounded_complete(agent, messages, facts, Explanation, "explanation", ("next_step_hint",), context_numbers,
                                   forbid_judgments=True, **kwargs)
