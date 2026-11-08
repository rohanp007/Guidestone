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
export const FIGURE_LABEL: Record<string, string> = { msrp: "Starting price", mpg_combined: "MPG (combined)", nhtsa_stars: "NHTSA safety stars", iihs_award: "IIHS award" };
export const FIGURES = ["msrp", "mpg_combined", "nhtsa_stars", "iihs_award"];
export const LOAN_TERM_MONTHS = 60;

export type Source = { figure: string; title: string; fetchedAt: string; url: string };
export type Pick = { id: string; name: string; why: string; sources: Source[] };
export type PlanRow = {
  code: string; name: string; down: number; loan: number; monthly: number; totalInterest: number; lowestCash: number; monthsBelow: number;
  cashAfter: number; chips: string[]; recommended: boolean; sameAsCash: boolean;
};
export type BuyView = {
  verdict: BuyVerdict; answer: string; choiceId: string;
  query: { segment: Segment; budgetBand: number | null; priorities: Priority[] };
  picks: Pick[];
  car: { name: string; price: number; apr: number; creditTier: string; savingsRate: number };
  plans: PlanRow[]; recommended: string;
};

const chips = (p: Plan): string[] => {
  const c: string[] = [];
  if (!p.payment_ok) c.push("Payment is high for your income");
  if (!p.cushion_holds) c.push("Cash dips below cushion");
  return c;
};

export function toBuyView(b: BuyAnalysis): BuyView {
  const pl = b.plans;
  // no_options (nothing fits the segment and budget) has no plans or chosen car: the page has no screen for it yet, so it takes its failed state
  if (b.no_options || !pl || !b.verdict || !b.chosen) throw new Error("no options");
  const order = ["cash", "finance", "mix"];
  return {
    verdict: b.verdict, answer: b.answer.text, choiceId: b.chosen,
    query: { segment: b.query.segment, budgetBand: b.query.budget_band ?? null, priorities: b.query.priorities ?? [] },
    picks: b.picks.map((p) => ({ id: p.id, name: p.name, why: p.why_text, sources: p.sources.map((s) => ({ figure: s.figure, title: s.source_title, fetchedAt: s.fetched_at, url: s.url })) })),
    car: { name: pl.vehicle_name, price: pl.price, apr: pl.loan_apr, creditTier: pl.credit_tier, savingsRate: pl.savings_rate },
    recommended: pl.recommended,
    plans: Object.values(pl.plans).sort((a, b2) => order.indexOf(a.code) - order.indexOf(b2.code)).map((p) => ({
      code: p.code, name: p.name, down: p.down_payment, loan: p.loan_amount, monthly: p.monthly_payment, totalInterest: p.total_interest,
      lowestCash: p.cushion.min_p10, monthsBelow: p.cushion.months_p10_below_cushion, cashAfter: p.cash_after, chips: chips(p),
      recommended: p.code === pl.recommended, sameAsCash: p.same_as_cash ?? false,
    })),
  };
}
