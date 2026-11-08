"use client";
import type { RecurringItem } from "@/lib/api";
import { dateShort, money2 } from "@/lib/format";
import { Section, Skeleton, Unavailable, type Load } from "./ui";

const GROUPS: { id: RecurringItem["group"]; title: string }[] = [
  { id: "income", title: "Income" }, { id: "bill", title: "Bills" }, { id: "debt", title: "Debt payments" },
];
const CADENCE: Record<string, string> = { monthly: "Monthly", semimonthly: "Twice a month", biweekly: "Every 2 weeks", irregular: "Irregular" };

export default function Recurring({ load }: { load: Load<RecurringItem[]> }) {
  return (
    <Section id="recurring-h" title="Recurring">
      {load.error && <Unavailable>Recurring payments are unavailable right now.</Unavailable>}
      {!load.data && !load.error && <div className="space-y-3">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-9 w-full" />)}</div>}
      <div className="fade-in space-y-7">
        {load.data && GROUPS.map((g) => {
          const items = load.data!.filter((i) => i.group === g.id);
          if (!items.length) return null;
          return (
            <div key={g.id}>
              <h3 className="border-b border-line pb-1.5 text-[15px] font-medium">{g.title}</h3>
              <ul>
                {items.map((i) => (
                  <li key={i.name} className="row-hover grid grid-cols-[1fr_auto] gap-x-4 border-b border-line-soft px-1 py-2.5">
                    <div className="min-w-0"><span className="block truncate text-[16px]">{i.name}</span><span className="text-[14px] text-ink-2">{CADENCE[i.cadence] ?? i.cadence} · next {dateShort(i.next_date)}</span></div>
                    <div className="num text-right text-[16px]">{money2(i.amount)}</div>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </div>
    </Section>
  );
}
