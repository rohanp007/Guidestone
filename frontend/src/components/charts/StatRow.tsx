import type { ReactNode } from "react";

export type StatItem = { label: string; value: ReactNode; note?: ReactNode };

/** A row of figure tiles: small caps label, large tabular number, optional note. */
export default function StatRow({ items }: { items: StatItem[] }) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-7 sm:grid-cols-3 xl:grid-cols-5">
      {items.map((i) => (
        <div key={i.label}>
          <dt className="label">{i.label}</dt>
          <dd className="num mt-1 text-[26px] font-medium leading-tight tracking-tight">{i.value}</dd>
          {i.note && <p className="mt-0.5 text-[14px] text-ink-2">{i.note}</p>}
        </div>
      ))}
    </dl>
  );
}
