/** The only file that reads fields of the simulate_buy response (same idea as lib/payoff.ts). */
import type { components } from "./api-types";
import type { AnalysisResult } from "./api";

export type BuyAnalysis = NonNullable<AnalysisResult["buy"]>;
type Plan = components["schemas"]["BuyPlan"];
export type Segment = "compact_suv" | "midsize_sedan";
export type Priority = "price" | "mpg" | "safety";
export type BuyVerdict = "affordable" | "stretch" | "not_now";
export type BuyParams = { segment: Segment; budget: number | null; priorities: Priority[]; choice_id?: string };

export const SEGMENT_LABEL: Record<Segment, string> = { compact_suv: "Compact SUV", midsize_sedan: "Midsize sedan" };
// the API takes three priorities; "Reliability" is not one of them, so it is not offered
export const PRIORITY_LABEL: Record<Priority, string> = { mpg: "Fuel economy", safety: "Safety", price: "Price" };
export const PRIORITIES: Priority[] = ["mpg", "safety", "price"];
export const VERDICT_LABEL: Record<BuyVerdict, string> = { affordable: "Affordable", stretch: "A stretch", not_now: "Not right now" };
export const FIGURE_LABEL: Record<string, string> = { msrp: "Starting price", model_year: "Model year", mpg_combined: "MPG (combined)", nhtsa_stars: "NHTSA safety stars", iihs_award: "IIHS award" };
export const FIGURES = ["msrp", "model_year", "mpg_combined", "nhtsa_stars", "iihs_award"];
export const LOAN_TERM_MONTHS = 60;

export type Figure = { value: number | string | null; source: { title: string; fetchedAt: string; url: string } | null };
export type Pick = { id: string; name: string; why: string; figures: Record<string, Figure> };
export type PlanRow = {
  code: string; name: string; down: number; loan: number; monthly: number; totalInterest: number; lowestCash: number | null; monthsBelow: number | null;
  cashAfter: number | null; infeasible: boolean; chips: string[]; recommended: boolean; sameAsCash: boolean; cushionFloor: number;
};
export type BandPoint = { t: number; p10: number; p50: number; p90: number };
export type BuyView = {
  noOptions: boolean;
  verdict: BuyVerdict | null; answer: string; choiceId: string;
  query: { segment: Segment; budgetBand: number | null; priorities: Priority[] };
  picks: Pick[];
  car: { name: string; price: number; apr: number; creditTier: string; savingsRate: number; aprSource: { title: string; fetchedAt: string; url: string } } | null;
  plans: PlanRow[]; recommended: string;
  bands: { baseline: BandPoint[]; scenario: BandPoint[]; scenarioLow: number[] } | null;   // scenarioLow: each month's lowest DAILY p10, the basis of the cushion facts
  caption: string;                                              // the chart caption, written by the API on that same basis
  highAprDebt: number | null;                                   // APR of the dearest debt still open in step 3, or null     // cash with and without the purchase, for the recommended plan
};

const WARNING_LABEL: Record<string, string> = {
  not_enough_cash: "Not enough cash", overdraft_risk: "Cash could go below $0", competes_with_high_apr_debt: "Cash could pay down high-rate debt instead",
};
const chips = (p: Plan): string[] => {
  const c = (p.warnings ?? []).map((w) => WARNING_LABEL[w.code] ?? w.code);
  if (p.status === "infeasible") return c;
  if (!p.payment_ok) c.push("Payment is high for your income");
  if (p.cushion_holds === false) c.push("Cash dips below cushion");
  return c;
};
const ref = (s: { source_title: string; fetched_at: string; url: string }) => ({ title: s.source_title, fetchedAt: s.fetched_at, url: s.url });
const day = (iso: string) => { const [y, m, d] = iso.slice(0, 10).split("-").map(Number); return new Date(y, m - 1, d).getTime(); };

export function toBuyView(b: BuyAnalysis): BuyView {
  const query = { segment: b.query.segment, budgetBand: b.query.budget_band ?? null, priorities: b.query.priorities ?? [] };
  const pl = b.plans;
  if (b.no_options || !pl || !b.verdict) {
    return { noOptions: true, verdict: null, answer: b.answer.text, choiceId: "", query, picks: [], car: null, plans: [], recommended: "", bands: null, caption: "", highAprDebt: null };
  }
  const order = ["cash", "finance", "mix"];
  const pt = (m: { day: string; p10: number; p50: number; p90: number }): BandPoint => ({ t: day(m.day), p10: m.p10, p50: m.p50, p90: m.p90 });
  return {
    noOptions: false, verdict: b.verdict, answer: b.answer.text, choiceId: b.chosen ?? "", query,
    picks: b.picks.map((p) => ({
      id: p.id, name: p.name, why: p.why_text,
      figures: Object.fromEntries(Object.entries(p.figures).map(([k, f]) => [k, { value: f.value ?? null, source: f.source ? ref(f.source) : null }])),
    })),
    car: { name: pl.vehicle_name, price: pl.price, apr: pl.loan_apr, creditTier: pl.credit_tier, savingsRate: pl.savings_rate, aprSource: ref(pl.apr_source) },
    recommended: pl.recommended,
    plans: Object.values(pl.plans).sort((a, c) => order.indexOf(a.code) - order.indexOf(c.code)).map((p) => ({
      code: p.code, name: p.name, down: p.down_payment, loan: p.loan_amount, monthly: p.monthly_payment, totalInterest: p.total_interest,
      lowestCash: p.cushion?.min_p10 ?? null, monthsBelow: p.cushion?.months_p10_below_cushion ?? null, cashAfter: p.cash_after ?? null,
      infeasible: p.status === "infeasible", chips: chips(p),
      recommended: p.code === pl.recommended, sameAsCash: p.same_as_cash ?? false, cushionFloor: p.cushion_floor,
    })),
    bands: pl.baseline_monthly?.length && pl.scenario_monthly?.length ? { baseline: pl.baseline_monthly.map(pt), scenario: pl.scenario_monthly.map(pt), scenarioLow: pl.scenario_month_low_p10 ?? [] } : null,
    caption: pl.caption ?? "", highAprDebt: pl.high_apr_debt_open ? pl.high_apr_debt_apr ?? null : null,
  };
}
