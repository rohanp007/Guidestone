/**
 * The ONLY file that reads fields of the simulate_payoff response. Everything else in the Pay Off page uses the PayoffView below, so a
 * backend rename (e.g. the cushion metrics moving from month-end to monthly daily minimum) is a one-file fix here.
 */
import type { PayoffAnalysis, Profile } from "./api";
import { parseDay } from "./format";

export type Verdict = "pay_it_off" | "pay_with_tradeoff" | "keep_cash" | "no_room" | "no_debt";
export type StrategyCode = "A" | "B" | "C" | "D" | "E";
export type WarningCode = "tradeoff_low_cushion" | "exceeds_safe_lump" | "lump_exceeds_debt";

export const VERDICT_LABEL: Record<Verdict, string> = {
  pay_it_off: "Pay it off", pay_with_tradeoff: "Pay it off, with a tradeoff", keep_cash: "Keep your cash",
  no_room: "No room right now", no_debt: "No debt to pay off",
};
export const STRATEGY_NAME: Record<StrategyCode, string> = {
  A: "Highest rate first", B: "Smallest balance first", C: "Lump sum only", D: "Lump sum, then highest rate", E: "Minimum payments only",
};
export const STRATEGY_CODES: StrategyCode[] = ["A", "B", "C", "D", "E"];
export const WARNING_LABEL: Record<WarningCode, string> = {
  tradeoff_low_cushion: "Cash dips below cushion", exceeds_safe_lump: "Uses more than your safe amount", lump_exceeds_debt: "Lump sum larger than the debt",
};

export type CompareRow = {
  code: StrategyCode; months: number | null; payoffDate: string | null; totalInterest: number; interestSaved: number;
  lowestCash: number | null; monthsBelowCushion: number | null; warnings: WarningCode[]; recommended: boolean;
};
export type CashPoint = { t: number; p10: number; p50: number; p90: number };
export type ScheduleLine = { month: number; date: string; payments: Record<string, number>; interest: number; remaining: number; totalPayment: number };

export type PayoffView = {
  verdict: Verdict;
  answer: string;
  chosen: StrategyCode | null;
  recommended: StrategyCode | null;
  extraMonthly: number;            // what the plan used (the surplus unless the request said otherwise)
  lumpSum: number;
  safeLumpSum: number;
  rows: CompareRow[];
  warnings: WarningCode[];         // for the chosen plan
  plan: null | {
    months: number | null; payoffDate: string | null; totalInterest: number; interestSaved: number; neverPaysOff: boolean;
    debtIds: string[]; schedule: ScheduleLine[];
    lowestCash: number | null; monthsBelowCushion: number;
  };
  cash: null | { baseline: CashPoint[]; plan: CashPoint[]; cushionTarget: number; belowTarget: boolean[] };
};

const KNOWN_WARNINGS = new Set<string>(["tradeoff_low_cushion", "exceeds_safe_lump", "lump_exceeds_debt"]);
const codes = (ws: { code: string }[] | undefined): WarningCode[] => (ws ?? []).map((w) => w.code).filter((c): c is WarningCode => KNOWN_WARNINGS.has(c));
const sumOf = (o: Record<string, number>) => Object.values(o).reduce((a, b) => a + b, 0);

type Strategy = NonNullable<NonNullable<PayoffAnalysis["simulation"]>["strategy"]>;

export function toSchedule(strategy: Strategy): ScheduleLine[] {
  return strategy.amortization.schedule.map((r) => ({ month: r.month, date: r.date, payments: r.payments, interest: sumOf(r.interest), remaining: sumOf(r.balances), totalPayment: r.total_payment }));
}

export function toView(p: PayoffAnalysis): PayoffView {
  const recommended = (p.recommended?.code ?? null) as StrategyCode | null;
  const chosen = (p.strategy_code ?? null) as StrategyCode | null;
  const rows: CompareRow[] = p.comparison.map((r) => ({
    code: r.code as StrategyCode, months: r.months, payoffDate: r.payoff_date, totalInterest: r.total_interest, interestSaved: r.interest_saved,
    lowestCash: r.cushion_min_p10, monthsBelowCushion: r.months_p10_below_cushion, warnings: codes(r.warnings), recommended: r.code === recommended,
  }));
  const strat = p.simulation?.strategy ?? null;
  const sim = p.simulation?.simulation ?? null;
  const am = strat?.amortization;
  const chosenRow = rows.find((r) => r.code === chosen);
  const warnings = [...new Set([...codes(strat?.warnings), ...(chosenRow?.warnings ?? [])])];

  let cash: PayoffView["cash"] = null;
  if (sim?.baseline_monthly && sim.scenario_monthly) {
    const pt = (m: { day: string; p10: number; p50: number; p90: number }): CashPoint => ({ t: parseDay(m.day).getTime(), p10: m.p10, p50: m.p50, p90: m.p90 });
    const plan = sim.scenario_monthly.map(pt);
    cash = { baseline: sim.baseline_monthly.map(pt), plan, cushionTarget: sim.cushion, belowTarget: plan.map((q) => q.p10 < sim.cushion) };
  }
  return {
    verdict: p.verdict, answer: p.answer.text, chosen, recommended,
    extraMonthly: p.extra_monthly, lumpSum: p.lump_sum, safeLumpSum: p.safe_lump_sum, rows, warnings,
    plan: strat && am ? {
      months: am.months_to_payoff, payoffDate: am.payoff_date, totalInterest: am.total_interest, interestSaved: strat.interest_saved_vs_minimum, neverPaysOff: am.never_pays_off,
      debtIds: am.order, schedule: toSchedule(strat),
      lowestCash: sim?.scenario_summary.min_p10 ?? null, monthsBelowCushion: sim?.scenario_summary.months_p10_below_cushion ?? 0,
    } : null,
    cash,
  };
}

/** Total remaining debt by month for one strategy's schedule (for the balance timeline). */
export const balanceSeries = (schedule: ScheduleLine[]) => schedule.map((r) => ({ t: parseDay(r.date).getTime(), v: r.remaining }));

const TYPE_NAME: Record<string, string> = { credit_card: "Credit card", auto_loan: "Auto loan", personal_loan: "Personal loan", mortgage: "Mortgage" };
/** A readable name for an account id in the schedule. */
export const debtName = (id: string, profile: Profile | null) => {
  const a = profile?.accounts.find((x) => x.id === id);
  return a ? TYPE_NAME[a.type] ?? a.type : id;
};
