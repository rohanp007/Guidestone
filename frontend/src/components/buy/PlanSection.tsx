"use client";
import { useMemo } from "react";
import { dateLong, dateShort, isoOf, money0, percent2 } from "@/lib/format";
import { LOAN_TERM_MONTHS, VERDICT_LABEL, type BuyVerdict, type BuyView } from "@/lib/buy";
import { Badge, ToneIcon, type Tone } from "@/components/accounts/ui";
import CompareTable, { type CompareRowData } from "@/components/charts/CompareTable";
import ForecastBand, { type BandSeries, type ShadedRange } from "@/components/charts/ForecastBand";
import StatRow from "@/components/charts/StatRow";

const TONE: Record<BuyVerdict, Tone> = { affordable: "good", stretch: "fair", not_now: "poor" };

function Chips({ items }: { items: string[] }) {
  if (!items.length) return <span className="text-ink-2">–</span>;
  return <span className="flex flex-wrap gap-1.5">{items.map((w) => <span key={w} className="inline-flex items-center gap-1.5 rounded-[6px] border border-line-strong bg-surface px-2 py-0.5 text-[13px]"><ToneIcon tone="fair" />{w}</span>)}</span>;
}

const MONTH_MS = 30.44 * 86_400_000;

function shadedBelow(points: { t: number; p10: number }[], floor: number): ShadedRange[] {
  const out: ShadedRange[] = [];
  const half = points.length > 1 ? (points[1].t - points[0].t) / 2 : MONTH_MS / 2;
  for (let i = 0; i < points.length; i++) {
    if (points[i].p10 >= floor) continue;
    let j = i; while (j + 1 < points.length && points[j + 1].p10 < floor) j++;
    out.push({ x1: i > 0 ? (points[i - 1].t + points[i].t) / 2 : points[i].t - half, x2: j < points.length - 1 ? (points[j].t + points[j + 1].t) / 2 : points[j].t + half, label: "Bad-luck cash is below the cushion" });
    i = j;
  }
  return out;
}

function CashChart({ view, floor, planName }: { view: BuyView; floor: number; planName: string }) {
  const bands = view.bands!;
  const series: BandSeries[] = useMemo(() => [
    { id: "base", label: "Without the purchase", points: bands.baseline, tone: "muted", band: true },
    { id: "plan", label: `With the purchase (${planName.toLowerCase()})`, points: bands.scenario, tone: "primary", band: true },
  ], [bands, planName]);
  // shading and caption use each month's lowest DAILY p10 (what "months below cushion" counts), not the month-end point
  const lows = bands.scenarioLow.length === bands.scenario.length ? bands.scenarioLow : bands.scenario.map((p) => p.p10);
  const shaded = useMemo(() => shadedBelow(bands.scenario.map((p, i) => ({ t: p.t, p10: lows[i] })), floor), [bands, lows, floor]);
  const end = bands.scenario[bands.scenario.length - 1], base = bands.baseline[bands.baseline.length - 1];
  const text = `By ${dateLong(isoOf(end.t))}, median cash is ${money0(end.p50)} with the purchase and ${money0(base.p50)} without it. ${view.caption}`;
  return (
    <ForecastBand series={series} summary={text} summaryText={text} shadedRanges={shaded} refLines={[{ y: floor, label: "Cushion", style: "dash" }, { y: 0, label: "$0", style: "zero" }]} />
  );
}

export default function PlanSection({ view }: { view: BuyView }) {
  const car = view.car!;
  const rows: CompareRowData[] = view.plans.map((p) => ({
    key: p.code, title: p.name, recommended: p.recommended, subtitle: p.sameAsCash ? "Same as paying cash" : undefined,
    cells: [
      { key: "down", label: "Down", value: money0(p.down), align: "right" },
      { key: "loan", label: "Loan", value: money0(p.loan), align: "right" },
      { key: "monthly", label: "Monthly", value: p.monthly > 0 ? money0(p.monthly) : "–", align: "right" },
      { key: "interest", label: "Interest", value: money0(p.totalInterest), align: "right" },
      { key: "cash", label: "Lowest cash", value: p.lowestCash === null ? "–" : money0(p.lowestCash), align: "right" },
      { key: "below", label: "Months below", value: p.monthsBelow ?? "–", align: "right" },
    ],
    notes: <Chips items={p.chips} />,
  }));
  const rec = view.plans.find((p) => p.recommended);
  return (
    <div className="space-y-8">
      <section aria-labelledby="buy-verdict-h">
        <h2 id="buy-verdict-h" className="label">Can you afford the {car.name}?</h2>
        <div className="mt-3"><Badge tone={TONE[view.verdict!]}>{VERDICT_LABEL[view.verdict!]}</Badge></div>
        <p className="mt-4 max-w-[62ch] text-[20px] font-medium leading-snug text-navy">{view.answer}</p>
      </section>
      <section aria-labelledby="buy-plans-h">
        <h2 id="buy-plans-h" className="label">Three ways to pay</h2>
        <div className="mt-4"><CompareTable rows={rows} highlightedKey={view.recommended} selectedKey={null} caption={`Ways to pay for the ${car.name}`} /></div>
        <p className="mt-4 text-[14px] text-ink-2">
          Financing uses <span className="num text-ink">{percent2(car.apr)}</span> APR ({car.creditTier.replace(/_/g, " ")} credit) on a {LOAN_TERM_MONTHS}-month loan.
          {" "}Source: <a href={car.aprSource.url} target="_blank" rel="noopener noreferrer" className="text-navy underline underline-offset-2">{car.aprSource.title}</a>, fetched {dateShort(car.aprSource.fetchedAt)}.
        </p>
      </section>
      {rec && view.bands && (
        <section aria-labelledby="buy-cash-h">
          <h2 id="buy-cash-h" className="label">Your cash with and without the purchase</h2>
          <div className="mt-4"><CashChart view={view} floor={rec.cushionFloor} planName={rec.name} /></div>
        </section>
      )}
      {rec && (
        <section aria-label="Recommended plan summary">
          <StatRow items={[
            { label: "Lowest cash", value: rec.lowestCash === null ? "–" : money0(rec.lowestCash), note: "In a tough month" },
            { label: "Months below cushion", value: rec.monthsBelow ?? "–" },
            { label: "Cash after buying", value: rec.cashAfter === null ? "–" : money0(rec.cashAfter) },
            { label: "Total interest", value: money0(rec.totalInterest) },
          ]} />
        </section>
      )}
    </div>
  );
}
