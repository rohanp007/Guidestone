"use client";
import type { Profile, Transaction } from "@/lib/api";
import { money0, percent } from "@/lib/format";
import { Section, Skeleton, Unavailable, type Load } from "./ui";

export const categoryName = (c: string) => c.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase());

export function topCategories(txs: Transaction[], n = 6) {
  const by = new Map<string, number>();
  for (const t of txs) by.set(t.category, (by.get(t.category) ?? 0) + t.amount);
  const total = [...by.values()].reduce((a, b) => a + b, 0);
  return { total, rows: [...by].sort((a, b) => b[1] - a[1]).slice(0, n).map(([category, amount]) => ({ category, amount, share: total ? amount / total : 0 })) };
}

/** `txs`: spending (flow_class "expense") for the last 3 months. `profile`: for the trend notes. */
export default function Spending({ txs, profile }: { txs: Load<Transaction[]>; profile: Load<Profile> }) {
  const { total, rows } = txs.data ? topCategories(txs.data) : { total: 0, rows: [] };
  const trends = profile.data?.spending?.notable_trends ?? [];
  const max = rows[0]?.amount ?? 1;
  return (
    <Section id="spending-h" title="Spending" aside="Last 3 months">
      {txs.error && <Unavailable>Spending is unavailable right now.</Unavailable>}
      {!txs.data && !txs.error && <div className="space-y-3">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-8 w-full" />)}</div>}
      {txs.data && (
        <div className="fade-in">
          <p className="text-[15px] text-ink-2"><span className="num text-ink">{money0(total / 3)}</span> a month on average, <span className="num text-ink">{money0(total)}</span> in all.</p>
          <ul className="mt-3">
            {rows.map((r) => (
              <li key={r.category} className="py-2">
                <div className="flex items-baseline justify-between gap-4 text-[16px]">
                  <span>{categoryName(r.category)}</span>
                  <span className="num">{money0(r.amount)} <span className="text-[13px] text-ink-2">{percent(r.share)}</span></span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-[2px] bg-line-soft" aria-hidden>
                  <div className="h-full origin-left bg-navy" style={{ width: "100%", transform: `scaleX(${r.amount / max})`, transition: "transform 250ms var(--ease)" }} />
                </div>
              </li>
            ))}
          </ul>
          {trends.length > 0 && (
            <div className="mt-5">
              <h3 className="border-b border-line pb-1.5 text-[15px] font-medium">Trends</h3>
              <ul className="mt-2 space-y-1 text-[15px]">
                {trends.map((t) => (
                  <li key={t.category}>{categoryName(t.category)} {t.annual_change >= 0 ? "up" : "down"} <span className="num">{Math.round(Math.abs(t.annual_change) * 100)}%</span>/yr
                    <span className="text-ink-2"> ({t.monthly_change >= 0 ? "+" : "−"}<span className="num">{money0(Math.abs(t.monthly_change))}</span>/mo)</span></li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}
