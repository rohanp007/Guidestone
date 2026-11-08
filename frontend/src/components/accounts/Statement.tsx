"use client";
import type { Profile } from "@/lib/api";
import { dateShort, dateLong, money0 } from "@/lib/format";
import CountUp from "./CountUp";

/** One factual sentence about the customer's money. */
export function headline(p: Profile): string {
  const s = p.summary;
  const ended = [...(p.ended_recurring ?? [])]
    .filter((r) => r.kind === "loan_payment")
    .sort((a, b) => b.last_date.localeCompare(a.last_date))[0];
  if (ended) {
    const days = (new Date(p.as_of_date).getTime() - new Date(ended.last_date).getTime()) / 86_400_000;
    if (days <= 120) return `Your ${ended.description.replace(/^PNC /, "").replace(/ Payment$/i, "").toLowerCase()} was last paid ${dateShort(ended.last_date)}, freeing ${money0(Math.abs(ended.monthly_amount))} a month.`;
  }
  if (!s) return "";
  if (s.monthly_surplus < 0) return `You spend ${money0(-s.monthly_surplus)} more than you take home each month.`;
  return `You keep ${money0(s.monthly_surplus)} of ${money0(s.net_monthly_income)} take-home each month after bills and spending.`;
}

export default function Statement({ profile }: { profile: Profile }) {
  const s = profile.summary;
  if (!s) return null;
  return (
    <section className="grid gap-6 sm:grid-cols-[1fr_auto] sm:items-end" aria-labelledby="nw-label">
      <div>
        <p id="nw-label" className="label">Net worth · as of {dateLong(profile.as_of_date)}</p>
        <p className="mt-2 text-[56px] font-semibold leading-none tracking-tight text-ink">
          <CountUp value={s.net_worth} format={money0} />
        </p>
        <p className="mt-4 max-w-[52ch] text-[16px] text-ink-2">{headline(profile)}</p>
      </div>
      <dl className="grid grid-cols-2 gap-6 sm:flex sm:gap-10">
        <div>
          <dt className="label">Liquid cash</dt>
          <dd className="mt-1 text-[32px] font-medium leading-tight"><CountUp value={s.liquid_cash} format={money0} /></dd>
        </div>
        <div>
          <dt className="label">Monthly surplus</dt>
          <dd className="mt-1 text-[32px] font-medium leading-tight text-orange"><CountUp value={s.monthly_surplus} format={money0} /></dd>
        </div>
      </dl>
    </section>
  );
}
