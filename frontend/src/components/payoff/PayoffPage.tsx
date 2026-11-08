"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { getAssumptions, getMyForecast, getMyProfile, type Me, type Profile } from "@/lib/api";
import { dateLong, dateShort, isoOf, money0, parseDay } from "@/lib/format";
import { balanceSeries, debtName, STRATEGY_NAME, VERDICT_LABEL, type StrategyCode, WARNING_LABEL, type CompareRow, type PayoffView, type Verdict, type WarningCode } from "@/lib/payoff";
import { Badge, Section, Skeleton, ToneIcon, Unavailable, useLoad, type Tone } from "@/components/accounts/ui";
import BalanceTimeline from "@/components/charts/BalanceTimeline";
import CompareTable, { type CompareRowData } from "@/components/charts/CompareTable";
import ForecastBand, { type BandSeries, type ShadedRange } from "@/components/charts/ForecastBand";
import StatRow from "@/components/charts/StatRow";
import PageShell from "@/components/shell/PageShell";
import Controls from "./Controls";
import { useMinimumOnly, usePayoff } from "./usePayoff";

const VERDICT_TONE: Record<Verdict, Tone> = { pay_it_off: "good", pay_with_tradeoff: "fair", keep_cash: "fair", no_room: "poor", no_debt: "good" };
const MONTH_MS = 30.44 * 86_400_000;
const monthName = (t: number) => new Intl.DateTimeFormat("en-US", { month: "short" }).format(t);

function Chips({ warnings }: { warnings: WarningCode[] }) {
  if (!warnings.length) return <span className="text-ink-2">–</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {warnings.map((w) => <span key={w} className="inline-flex items-center gap-1.5 rounded-[6px] border border-line-strong bg-surface px-2 py-0.5 text-[13px]"><ToneIcon tone="fair" />{WARNING_LABEL[w]}</span>)}
    </span>
  );
}

function Verdict({ view }: { view: PayoffView }) {
  const chosen = view.chosen;
  return (
    <section aria-labelledby="verdict-h" className="py-8 sm:py-10">
      <h2 id="verdict-h" className="label">Should you pay it off?</h2>
      <div className="mt-3"><Badge tone={VERDICT_TONE[view.verdict]}>{VERDICT_LABEL[view.verdict]}</Badge></div>
      <p className="mt-4 max-w-[62ch] text-[20px] font-medium leading-snug text-navy">{view.answer}</p>
      {chosen && (
        <p className="mt-3 text-[15px] text-ink-2">
          Plan shown: <span className="font-medium text-ink">{chosen} · {STRATEGY_NAME[chosen]}</span>
          {chosen === view.recommended && <span className="ml-2 rounded-[6px] border border-navy px-1.5 py-px text-[12px] font-medium text-navy">Recommended</span>}
        </p>
      )}
    </section>
  );
}

function rangesFor(points: { t: number }[], below: boolean[]): ShadedRange[] {
  const out: ShadedRange[] = [];
  const half = points.length > 1 ? (points[1].t - points[0].t) / 2 : MONTH_MS / 2;
  for (let i = 0; i < points.length; i++) {
    if (!below[i]) continue;
    let j = i; while (j + 1 < points.length && below[j + 1]) j++;
    out.push({ x1: i > 0 ? (points[i - 1].t + points[i].t) / 2 : points[i].t - half, x2: j < points.length - 1 ? (points[j].t + points[j + 1].t) / 2 : points[j].t + half, label: "Plan’s bad-luck cash is below the cushion target" });
    i = j;
  }
  return out;
}

function CashChart({ view, oneMonthCushion }: { view: PayoffView; oneMonthCushion: number | null }) {
  const cash = view.cash!;
  const series: BandSeries[] = useMemo(() => [
    { id: "base", label: "Without the plan", points: cash.baseline, tone: "muted", band: true },
    { id: "plan", label: "With the plan", points: cash.plan, tone: "primary", band: true },
  ], [cash]);
  const shaded = useMemo(() => rangesFor(cash.plan, cash.belowTarget), [cash]);
  const lowMonths = cash.plan.filter((_, i) => cash.belowTarget[i]).map((p) => monthName(p.t));
  const base = cash.baseline[cash.baseline.length - 1], plan = cash.plan[cash.plan.length - 1];
  const minPlan = Math.min(...cash.plan.map((p) => p.p10)), minBase = Math.min(...cash.baseline.map((p) => p.p10));
  const text = `By ${dateLong(isoOf(plan.t))}, median cash is ${money0(plan.p50)} with the plan and ${money0(base.p50)} without it. In a bad stretch (10th percentile) cash falls to ${money0(minPlan)} with the plan, against ${money0(minBase)} without it. ` +
    (lowMonths.length ? `That is below the ${money0(cash.cushionTarget)} cushion target at the end of ${lowMonths.join(", ")}.` : `That stays above the ${money0(cash.cushionTarget)} cushion target.`);
  const refLines = [
    { y: cash.cushionTarget, label: "Cushion target", style: "dash" as const },
    ...(oneMonthCushion !== null ? [{ y: oneMonthCushion, label: "1-month cushion", style: "dotted" as const }] : []),
    { y: 0, label: "$0", style: "zero" as const },
  ];
  return (
    <ForecastBand series={series} refLines={refLines} shadedRanges={shaded} summary={text} summaryText={text} heightClass="h-[260px] sm:h-[300px]">
      <p className="mt-3 text-[13px] text-ink-2">Month-end cash across 1,000 simulated paths. The shaded months are where the bad-luck case (10th percentile) falls under the cushion target.</p>
    </ForecastBand>
  );
}

function BalanceChart({ view, minimum }: { view: PayoffView; minimum: PayoffView | null }) {
  const plan = view.plan!;
  const code = view.chosen ?? "A";
  const own = balanceSeries(plan.schedule);
  const cutoff = plan.payoffDate ? parseDay(plan.payoffDate).getTime() + 12 * MONTH_MS : Infinity;
  const showMin = !!minimum?.plan && view.chosen !== "E";
  const min = showMin ? balanceSeries(minimum!.plan!.schedule) : [];
  const capped = min.filter((p) => p.t <= cutoff);
  const seriesByStrategy = { [code]: own, ...(showMin ? { E: capped } : {}) };
  const labels = { [code]: STRATEGY_NAME[code], E: "Minimum payments only" };
  const end = plan.payoffDate ? parseDay(plan.payoffDate).getTime() : null;
  const minAtEnd = end !== null && min.length ? min.reduce((b, p) => (Math.abs(p.t - end) < Math.abs(b.t - end) ? p : b), min[0]).v : null;
  const text = plan.neverPaysOff
    ? `With ${STRATEGY_NAME[code].toLowerCase()}, this plan does not pay off your debt within 30 years.`
    : `With ${STRATEGY_NAME[code].toLowerCase()}, your total debt is ${money0(own[0]?.v ?? 0)} after the first payment and reaches $0 on ${plan.payoffDate ? dateLong(plan.payoffDate) : "–"}.` +
      (minAtEnd !== null ? ` With minimum payments only you would still owe ${money0(minAtEnd)} then.` : "");
  return (
    <BalanceTimeline seriesByStrategy={seriesByStrategy} labels={labels} emphasized={code} summary={text} summaryText={text}
      markers={end !== null && !plan.neverPaysOff ? [{ t: end, label: "Debt-free" }] : []}>
      {showMin && minimum!.plan!.payoffDate && (
        <p className="mt-3 text-[13px] text-ink-2">Minimum payments: debt-free in {dateLong(minimum!.plan!.payoffDate)}{min[min.length - 1] && min[min.length - 1].t > cutoff ? " (the chart stops a year after your plan finishes)" : ""}.</p>
      )}
    </BalanceTimeline>
  );
}

function Schedule({ view, profile }: { view: PayoffView; profile: Profile | null }) {
  const plan = view.plan!;
  return (
    <details className="group">
      <summary className="cursor-pointer select-none text-[15px] font-medium text-navy">Month-by-month schedule ({plan.schedule.length} {plan.schedule.length === 1 ? "month" : "months"})</summary>
      <div className="mt-3 max-h-[420px] overflow-auto">
        <table className="num w-full min-w-[560px] text-left text-[14px]">
          <thead className="sticky top-0 bg-bg"><tr className="label border-b border-line-strong">
            <th scope="col" className="py-2 pr-4 font-normal">Month</th>
            {plan.debtIds.map((id) => <th key={id} scope="col" className="pr-4 text-right font-normal">{debtName(id, profile)}</th>)}
            <th scope="col" className="pr-4 text-right font-normal">Interest</th><th scope="col" className="text-right font-normal">Remaining</th>
          </tr></thead>
          <tbody>
            {plan.schedule.map((r) => (
              <tr key={r.month} className="border-b border-line-soft">
                <th scope="row" className="py-2 pr-4 text-left font-normal">{r.month === 0 ? "Now" : dateShort(r.date)}<span className="ml-2 text-ink-2">{r.month === 0 ? "" : new Date(parseDay(r.date)).getFullYear()}</span></th>
                {plan.debtIds.map((id) => <td key={id} className="pr-4 text-right">{money0(r.payments[id] ?? 0)}</td>)}
                <td className="pr-4 text-right">{money0(r.interest)}</td><td className="text-right">{money0(r.remaining)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function AssumptionsList() {
  const { data, error } = useLoad(getAssumptions);
  if (error) return <Unavailable>The assumptions are unavailable right now.</Unavailable>;
  if (!data) return <Skeleton className="mt-3 h-24 w-full" />;
  return (
    <dl className="mt-3 grid gap-x-6 gap-y-2 text-[14px] sm:grid-cols-2">
      {Object.entries(data.assumptions).map(([k, v]) => (
        <div key={k}><dt className="text-ink-2">{k.replace(/_/g, " ")}</dt><dd><span className="num">{String(v.value)}</span> <span className="text-ink-2">· {String(v.source)}</span></dd></div>
      ))}
    </dl>
  );
}

function AssumptionsNote() {
  const [open, setOpen] = useState(false);
  return (
    <div className="text-[14px] text-ink-2">
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)} className="font-medium text-navy underline-offset-2 hover:underline">Rates and thresholds we used</button>
      {" "}— the simulation uses these assumptions, not a promise of what will happen.
      {open && <AssumptionsList />}
    </div>
  );
}

function Body({ me }: { me: Me }) {
  const p = usePayoff();
  const profile = useLoad(getMyProfile);
  const forecast = useLoad(getMyForecast);
  const view = p.view;
  const minimum = useMinimumOnly(!!view && !!view.plan && view.chosen !== "E");

  if (p.firstFailed) return <div className="py-10"><Unavailable>The payoff analysis is unavailable right now. Your accounts are still available.</Unavailable></div>;
  if (!view) return <div className="space-y-6 py-10" aria-busy="true"><Skeleton className="h-8 w-40" /><Skeleton className="h-16 w-full max-w-[62ch]" /><Skeleton className="h-64 w-full" /></div>;

  if (view.verdict === "no_debt") {
    return (
      <div className="fade-in">
        <Verdict view={view} />
        <Link href="/accounts" className="text-[15px] font-medium text-navy underline-offset-2 hover:underline">← Back to your accounts</Link>
      </div>
    );
  }

  const plan = view.plan;
  const mode = view.verdict === "no_room" ? "extra_only" : "full";
  const rows: CompareRowData[] = view.rows.map((r: CompareRow) => ({
    key: r.code, title: `${r.code} · ${STRATEGY_NAME[r.code]}`, recommended: r.recommended,
    cells: [
      { key: "months", label: "Months", value: r.months === null ? "Never" : r.months, align: "right" },
      { key: "interest", label: "Total interest", value: money0(r.totalInterest), align: "right" },
      { key: "saved", label: "Interest saved", value: money0(r.interestSaved), align: "right" },
      { key: "cash", label: "Lowest cash", value: r.lowestCash === null ? "–" : money0(r.lowestCash), align: "right" },
      { key: "below", label: "Months below cushion", value: r.monthsBelowCushion ?? "–", align: "right" },
    ],
    notes: <Chips warnings={r.warnings} />,
  }));

  return (
    <div className="fade-in">
      <Verdict view={view} />
      <div className="grid gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_340px]">
        <aside className="lg:order-last">
          <div className="lg:sticky lg:top-4">
            <Controls controls={p.controls} view={view} defaults={p.defaults} totalCash={me.summary?.liquid_cash ?? 0} mode={mode} onChange={p.set} onReset={p.reset} loading={p.loading} failed={p.failed} />
          </div>
        </aside>
        <div className="min-w-0 space-y-12 transition-opacity duration-200" style={{ opacity: p.loading ? 0.5 : 1, transitionTimingFunction: "var(--ease)" }} aria-busy={p.loading}>
          {view.verdict === "keep_cash" && plan && <p className="text-[15px] text-ink-2">What if you paid it down anyway? Here is what that would look like.</p>}
          {plan && (
            <section aria-label="Summary">
              <StatRow items={[
                { label: "Debt-free", value: plan.neverPaysOff || !plan.payoffDate ? "Not in 30 years" : dateLong(plan.payoffDate) },
                { label: "Months to payoff", value: plan.neverPaysOff || plan.months === null ? "–" : plan.months, note: plan.months === 0 ? "Paid off right away" : undefined },
                { label: "Total interest", value: money0(plan.totalInterest) },
                { label: "Interest saved", value: money0(plan.interestSaved), note: "vs minimum payments" },
                { label: "Lowest cash", value: plan.lowestCash === null ? "–" : money0(plan.lowestCash), note: plan.monthsBelowCushion > 0 ? `Below your cushion for ${plan.monthsBelowCushion} ${plan.monthsBelowCushion === 1 ? "month" : "months"}` : "In a bad stretch" },
              ]} />
            </section>
          )}
          {rows.length > 0 && (
            <Section id="compare-h" title="Compare strategies">
              <CompareTable rows={rows} highlightedKey={view.recommended} selectedKey={view.chosen} onSelect={(k) => p.set({ strategy: k as StrategyCode })} caption="Payoff strategies compared" />
            </Section>
          )}
          {plan && (
            <Section id="balance-h" title="Debt over time"><BalanceChart view={view} minimum={minimum} /></Section>
          )}
          {view.cash && (
            <Section id="cash-h" title="Your cash with and without the plan"><CashChart view={view} oneMonthCushion={forecast.data?.cushion_risk.buffer ?? null} /></Section>
          )}
          {plan && (
            <Section id="schedule-h" title="Schedule"><Schedule view={view} profile={profile.data} /></Section>
          )}
          <AssumptionsNote />
        </div>
      </div>
    </div>
  );
}

export default function PayoffPage() {
  return <PageShell active="simulate" title="Pay off debt">{(me) => <Body me={me} />}</PageShell>;
}
