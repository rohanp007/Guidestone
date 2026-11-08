"use client";
import type { Profile, Transaction } from "@/lib/api";
import { money0, percent } from "@/lib/format";
import { Section, Skeleton, Unavailable, type Load } from "./ui";

export const categoryName = (c: string) => c.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase());

const MONTHS = 3;
type Row = { category: string; label: string; monthly: number; share: number };

/** Top categories as monthly averages over the 3-month window, plus an "Everything else" row so the shares add up to 100%.
 *  `txs` must already be spending only (flow_class "expense": no transfers, card payments or interest). */
export function topCategories(txs: Transaction[], n = 6): { monthlyTotal: number; total: number; rows: Row[] } {
  const by = new Map<string, number>();
  for (const t of txs) by.set(t.category, (by.get(t.category) ?? 0) + t.amount);
  const total = [...by.values()].reduce((a, b) => a + b, 0);
  const sorted = [...by].sort((a, b) => b[1] - a[1]);
  const top = sorted.slice(0, n).map(([category, amount]): Row => ({ category, label: categoryName(category), monthly: amount / MONTHS, share: total ? amount / total : 0 }));
  const rest = sorted.slice(n);
  if (rest.length) {
    const sumTop = top.reduce((a, r) => a + Math.round(r.share * 1000), 0);   // tenths of a percent, so the displayed shares add up to exactly 100.0%
    top.push({ category: "_other", label: "Everything else", monthly: rest.reduce((a, [, v]) => a + v, 0) / MONTHS, share: (1000 - sumTop) / 1000 });
  }
  return { monthlyTotal: total / MONTHS, total, rows: top };
}

/** `txs`: spending (flow_class "expense") for the last 3 months. `profile`: for the trend notes. */
export default function Spending({ txs, profile }: { txs: Load<Transaction[]>; profile: Load<Profile> }) {
  const { monthlyTotal, rows } = txs.data ? topCategories(txs.data) : { monthlyTotal: 0, rows: [] };
  const trends = profile.data?.spending?.notable_trends ?? [];
  const max = Math.max(...rows.map((r) => r.monthly), 1);
  return (
    <Section id="spending-h" title="Spending" aside="Monthly average, last 3 months">
      {txs.error && <Unavailable>Spending is unavailable right now.</Unavailable>}
      {!txs.data && !txs.error && <div className="space-y-3">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-8 w-full" />)}</div>}
      {txs.data && (
        <div className="fade-in">
          <p className="text-[15px] text-ink-2"><span className="num text-ink">{money0(monthlyTotal)}</span> a month on average.</p>
          <ul className="mt-3">
            {rows.map((r) => (
              <li key={r.category} className="py-2">
                <span className="sr-only">{`${r.label}, ${money0(r.monthly)} a month, ${percent(r.share)}`}</span>
                <div aria-hidden className="flex items-baseline justify-between gap-6 text-[16px]">
                  <span>{r.label}</span>
                  <span className="flex items-baseline gap-3 whitespace-nowrap">
                    <span className="num">{money0(r.monthly)}<span className="text-[13px] text-ink-2">/mo</span></span>
                    <span className="num w-14 text-right text-[13px] text-ink-2">{percent(r.share)}</span>
                  </span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-[2px] bg-line-soft" aria-hidden>
                  <div className="h-full origin-left bg-navy" style={{ width: "100%", transform: `scaleX(${r.monthly / max})`, transition: "transform 250ms var(--ease)" }} />
                </div>
              </li>
            ))}
          </ul>
          {trends.length > 0 && (
            <div className="mt-5">
              <h3 className="border-b border-line pb-1.5 text-[15px] font-medium">Trends</h3>
              <ul className="mt-2 space-y-1 text-[15px]">
                {trends.map((t) => (
                  <li key={t.category}>{categoryName(t.category)} {t.annual_change >= 0 ? "up" : "down"} <span className="num">{Math.round(Math.abs(t.annual_change) * 100)}%</span> a year
                    <span className="text-ink-2"> (<span className="num">{money0(Math.abs(t.monthly_change))}</span> a month {t.monthly_change >= 0 ? "more" : "less"} after a year)</span></li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}
