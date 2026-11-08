"use client";
import { useState } from "react";
import { getAssumptions } from "@/lib/api";
import { money0, percent2 } from "@/lib/format";
import { LOAN_TERM_MONTHS, VERDICT_LABEL, type BuyVerdict, type BuyView } from "@/lib/buy";
import { Badge, Skeleton, ToneIcon, useLoad, type Tone } from "@/components/accounts/ui";
import CompareTable, { type CompareRowData } from "@/components/charts/CompareTable";
import StatRow from "@/components/charts/StatRow";

const TONE: Record<BuyVerdict, Tone> = { affordable: "good", stretch: "fair", not_now: "poor" };

function Chips({ items }: { items: string[] }) {
  if (!items.length) return <span className="text-ink-2">–</span>;
  return <span className="flex flex-wrap gap-1.5">{items.map((w) => <span key={w} className="inline-flex items-center gap-1.5 rounded-[6px] border border-line-strong bg-surface px-2 py-0.5 text-[13px]"><ToneIcon tone="fair" />{w}</span>)}</span>;
}

function RateSource() {
  const { data } = useLoad(getAssumptions);
  const src = data?.assumptions?.car_loan_term_months?.source;
  if (!data) return <Skeleton className="mt-2 h-10 w-full" />;
  return <p className="mt-2 text-[13px] text-ink-2">{src ? String(src) : "Not available"}</p>;
}

export default function PlanSection({ view }: { view: BuyView }) {
  const [showSource, setShowSource] = useState(false);
  const rows: CompareRowData[] = view.plans.map((p) => ({
    key: p.code, title: p.name, recommended: p.recommended, subtitle: p.sameAsCash ? "Same as paying cash" : undefined,
    cells: [
      { key: "down", label: "Down", value: money0(p.down), align: "right" },
      { key: "loan", label: "Loan", value: money0(p.loan), align: "right" },
      { key: "monthly", label: "Monthly", value: p.monthly > 0 ? money0(p.monthly) : "–", align: "right" },
      { key: "interest", label: "Interest", value: money0(p.totalInterest), align: "right" },
      { key: "cash", label: "Lowest cash", value: money0(p.lowestCash), align: "right" },
      { key: "below", label: "Months below", value: p.monthsBelow, align: "right" },
    ],
    notes: <Chips items={p.chips} />,
  }));
  const rec = view.plans.find((p) => p.recommended);
  return (
    <div className="space-y-8">
      <section aria-labelledby="buy-verdict-h">
        <h2 id="buy-verdict-h" className="label">Can you afford the {view.car.name}?</h2>
        <div className="mt-3"><Badge tone={TONE[view.verdict]}>{VERDICT_LABEL[view.verdict]}</Badge></div>
        <p className="mt-4 max-w-[62ch] text-[20px] font-medium leading-snug text-navy">{view.answer}</p>
      </section>
      <section aria-labelledby="buy-plans-h">
        <h2 id="buy-plans-h" className="label">Three ways to pay</h2>
        <div className="mt-4"><CompareTable rows={rows} highlightedKey={view.recommended} selectedKey={null} caption={`Ways to pay for the ${view.car.name}`} /></div>
        <p className="mt-4 text-[14px] text-ink-2">
          Financing uses <span className="num text-ink">{percent2(view.car.apr)}</span> APR ({view.car.creditTier.replace(/_/g, " ")} credit) on a {LOAN_TERM_MONTHS}-month loan.{" "}
          <button type="button" aria-expanded={showSource} onClick={() => setShowSource((s) => !s)} className="font-medium text-navy underline-offset-2 hover:underline">Where this rate comes from</button>
        </p>
        {showSource && <RateSource />}
      </section>
      {rec && (
        <section aria-label="Recommended plan summary">
          <StatRow items={[
            { label: "Lowest cash", value: money0(rec.lowestCash), note: "In a tough month" },
            { label: "Months below cushion", value: rec.monthsBelow },
            { label: "Cash after buying", value: money0(rec.cashAfter) },
            { label: "Total interest", value: money0(rec.totalInterest) },
          ]} />
        </section>
      )}
    </div>
  );
}
