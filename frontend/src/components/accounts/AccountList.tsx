"use client";
import type { ReactNode } from "react";
import type { Profile } from "@/lib/api";
import { dateLong, dateShort, money0, money2, percent, percent2 } from "@/lib/format";

type Account = Profile["accounts"][number];
type Row = { key: string; name: string; sub?: string; details: ReactNode; amount: ReactNode; estimated?: boolean; muted?: boolean };
type Group = { title: string; total?: number; rows: Row[] };

const NAMES: Record<string, string> = {
  checking: "Checking", savings: "Savings", cd: "Certificate of deposit", ira: "IRA", credit_card: "Credit card",
  auto_loan: "Auto loan", personal_loan: "Personal loan", mortgage: "Mortgage", home: "Home", vehicle: "Vehicle",
};
const title = (s: string) => s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

const Detail = ({ children }: { children: ReactNode }) => <span className="mono text-[13px] text-ink-2">{children}</span>;

function details(a: Account): ReactNode {
  const bits: ReactNode[] = [];
  if (a.type === "credit_card" && a.credit_limit) {
    const u = Math.min(1, a.balance / a.credit_limit);
    bits.push(
      <span key="u" className="inline-flex items-center gap-2">
        <span className="h-1.5 w-20 overflow-hidden rounded-full bg-line" role="img" aria-label={`${percent(u)} of limit used`}>
          <span className="block h-full origin-left rounded-full bg-navy" style={{ width: "100%", transform: `scaleX(${u})` }} />
        </span>
        <Detail>{percent(u)} of {money0(a.credit_limit)}</Detail>
      </span>,
    );
  }
  if (a.rate) bits.push(<Detail key="r">{a.type === "ira" ? "Rate " : ""}{percent2(a.rate)}{a.type === "savings" || a.type === "cd" ? " APY" : a.type === "ira" ? "" : " APR"}</Detail>);
  if (a.term_months_remaining) bits.push(<Detail key="t">{a.term_months_remaining} mo left</Detail>);
  if (a.maturity_date) bits.push(<Detail key="m">Matures {dateLong(a.maturity_date)}</Detail>);
  if (a.min_payment) bits.push(<Detail key="p">Min {money0(a.min_payment)}</Detail>);
  return <span className="flex flex-wrap items-center gap-x-4 gap-y-1">{bits}</span>;
}

function build(profile: Profile): Group[] {
  const live = profile.accounts;
  const mk = (a: Account, extra?: Partial<Row>): Row => ({
    key: a.id,
    name: a.type === "ira" ? `${title(a.subtype || "")} IRA`.trim() : NAMES[a.type] ?? title(a.type),
    sub: a.subtype && a.subtype !== "estimated" && a.type !== "ira" ? title(a.subtype) : undefined,
    details: details(a),
    amount: money2(a.balance),
    ...extra,
  });
  const of = (...t: string[]) => live.filter((a) => t.includes(a.type));
  const sum = (xs: Account[]) => xs.reduce((n, a) => n + a.balance, 0);

  const cash = of("checking", "savings"), inv = of("cd", "ira"), debt = of("credit_card", "auto_loan", "personal_loan", "mortgage").filter((a) => a.balance > 0);
  const est = of("home", "vehicle");
  const paid = profile.ended_recurring ?? [];

  const groups: Group[] = [
    { title: "Cash", total: sum(cash), rows: cash.map((a) => mk(a)) },
    { title: "Investments", total: sum(inv), rows: inv.map((a) => mk(a)) },
    { title: "Debt", total: sum(debt), rows: debt.map((a) => mk(a)) },
    { title: "Estimated assets", total: sum(est), rows: est.map((a) => mk(a, { estimated: true })) },
    {
      title: "Recently paid off",
      rows: paid.map((r, i) => ({
        key: `paid-${i}`, name: r.description.replace(/^PNC /, ""), muted: true,
        details: <Detail>Last payment {dateShort(r.last_date)}</Detail>,
        amount: <span>{money0(Math.abs(r.monthly_amount))}<span className="text-[13px]">/mo</span></span>,
      })),
    },
  ];
  return groups.filter((g) => g.rows.length);
}

export default function AccountList({ profile }: { profile: Profile }) {
  return (
    <section aria-label="Accounts" className="space-y-10">
      {build(profile).map((g) => (
        <div key={g.title}>
          <div className="flex items-baseline justify-between border-b border-line-strong pb-2">
            <h2 className="label">{g.title}</h2>
            {g.total !== undefined && <span className="mono text-[13px] text-ink-2">{money2(g.total)}</span>}
          </div>
          <ul>
            {g.rows.map((r) => (
              <li key={r.key} className={`row-hover grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 border-b border-line px-1 py-3 sm:grid-cols-[minmax(160px,1.1fr)_2fr_150px] ${r.muted ? "text-ink-3" : ""}`}>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className={`truncate text-[16px] font-medium ${r.muted ? "" : "text-ink"}`}>{r.name}</span>
                    {r.estimated && <span className="rounded-[6px] border border-line-strong px-1.5 py-px text-[11px] text-ink-2">Estimated</span>}
                  </div>
                  {r.sub && <div className="text-[13px] text-ink-3">{r.sub}</div>}
                </div>
                <div className="order-3 col-span-2 sm:order-none sm:col-span-1">{r.details}</div>
                <div className={`text-right text-[16px] font-medium ${r.muted ? "text-ink-3" : "text-ink"}`}>{r.amount}</div>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
