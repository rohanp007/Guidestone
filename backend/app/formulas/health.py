"""Financial health formulas."""
from __future__ import annotations

from ..data.models import Base, Profile
from .registry import register

# --- paycheck_to_paycheck: provisional weights; tune against real customers. Cutoffs sit in natural gaps of the demo data
# (17 customers have a minimum buffer below 0.007, the next is 0.026).
BUFFER_NEAR_ZERO = 0.02      # lowest checking balance / month's outflow below this = effectively ran out of cash
BUFFER_THIN_MEDIAN = 0.25    # typical month's low point under a quarter of a month's outflow
THIN_SURPLUS_SHARE = 0.05    # surplus under 5% of take-home pay
PULLS_PER_YEAR = 2           # covering shortfalls from savings at least this often
WEIGHTS = {"topups": 0.45, "buffer_min": 0.35, "pulls": 0.10, "buffer_median": 0.10, "thin_surplus": 0.10}
FLAG_AT = 0.5                # the two strong signals (top-ups, near-zero buffer) can flag on their own; the soft ones cannot


class PaycheckToPaycheck(Base):
    flag: bool
    score: float                 # 0-1
    reasons: list[str]           # plain-language evidence, strongest first, for the advisor to cite


@register("paycheck_to_paycheck", result=PaycheckToPaycheck, category="health")
def paycheck_to_paycheck(profile: Profile) -> PaycheckToPaycheck:
    """Is this customer living paycheck to paycheck?

    Strong inputs: outside-account top-ups into checking (overdraft_topups) and a near-zero minimum buffer. Together they
    always flag. A near-zero buffer alone also needs a little corroboration (frequent savings pulls, a thin typical
    buffer, or a thin surplus). Soft signals alone never flag.
    """
    sp, s = profile.spending, profile.summary
    if sp is None or s is None:
        raise ValueError(f"{profile.id}: needs derive.enrich() and a summary first")

    score, reasons = 0.0, []
    if sp.overdraft_topups >= 1:
        score += WEIGHTS["topups"]
        reasons.append(f"needed {sp.overdraft_topups} deposit(s) from an outside account to cover checking shortfalls")
    if sp.buffer_ratio_min < BUFFER_NEAR_ZERO:
        score += WEIGHTS["buffer_min"]
        reasons.append(f"checking fell to {sp.buffer_ratio_min:.1%} of a month's outflow at its lowest")
    if sp.savings_pulls_per_year >= PULLS_PER_YEAR:
        score += WEIGHTS["pulls"]
        reasons.append(f"pulled from savings {sp.savings_pulls_per_year:.0f} times a year to cover checking")
    if sp.buffer_ratio_median < BUFFER_THIN_MEDIAN:
        score += WEIGHTS["buffer_median"]
        reasons.append(f"a typical month's low point is only {sp.buffer_ratio_median:.0%} of a month's outflow")
    if s.monthly_surplus < THIN_SURPLUS_SHARE * profile.net_monthly:
        score += WEIGHTS["thin_surplus"]
        reasons.append(f"surplus is {s.monthly_surplus / profile.net_monthly:.0%} of take-home pay")

    return PaycheckToPaycheck(flag=score >= FLAG_AT, score=min(1.0, score), reasons=reasons)
