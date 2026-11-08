"use client";
import Link from "next/link";
import type { ReactNode } from "react";
import type { Profile } from "@/lib/api";
import { dateLong, money0, money2, percent, percent2 } from "@/lib/format";
import { Section, Skeleton, Unavailable, type Load } from "./ui";

type Account = Profile["accounts"][number];
type Row = { payoff?: boolean; key: string; name: string; sub?: string; facts: ReactNode[]; amount: number; owed?: boolean; estimated?: boolean };
type Group = { title: string; rows: Row[] };

const NAMES: Record<string, string> = {
  checking: "Checking", savings: "Savings", cd: "Certificate of deposit", ira: "IRA", credit_card: "Credit card",
  auto_loan: "Auto loan", personal_loan: "Personal loan", mortgage: "Mortgage", home: "Home", vehicle: "Vehicle",
};
const title = (s: string) => s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const Fact = ({ children }: { children: ReactNode }) => <span className="num text-[14px] text-ink-2">{children}</span>;

function UtilBar({ used, limit }: { used: number; limit: number }) {
  const u = Math.min(1, used / limit);
  return (
    <span className="inline-flex items-center gap-2">
      <span className="h-1.5 w-20 overflow-hidden rounded-full bg-line" role="img" aria-label={`${percent(u)} of limit used`}>
        <span className="block h-full origin-left rounded-full bg-navy" style={{ width: "100%", transform: `scaleX(${u})`, transition: "transform 250ms var(--ease)" }} />
      </span>
      <Fact>{percent(u)} of {money0(limit)} used</Fact>
    </span>
  );
}

function row(a: Account, profile: Profile): Row {
  const facts: ReactNode[] = [];
  const paidInFull = profile.spending ? !profile.spending.carries_card_balance : false;
  if (a.type === "credit_card") {
    facts.push(<Fact key="apr">{percent(a.rate ?? 0)} APR</Fact>);
    if (a.credit_limit) facts.push(<UtilBar key="u" used={a.balance} limit={a.credit_limit} />);
    if (paidInFull) facts.push(<Fact key="pif">Paid in full monthly</Fact>);
  } else if (a.type === "auto_loan" || a.type === "personal_loan" || a.type === "mortgage") {
    facts.push(<Fact key="apr">{percent(a.rate ?? 0)} APR</Fact>);
    if (a.min_payment) facts.push(<Fact key="pay">{money0(a.min_payment)}/mo payment</Fact>);
    if (a.maturity_date) facts.push(<Fact key="end">Paid off {dateLong(a.maturity_date)}</Fact>);
    else if (a.term_months_remaining) facts.push(<Fact key="end">{a.term_months_remaining} months left</Fact>);
  } else if (a.type === "savings") {
    if (a.rate) facts.push(<Fact key="y">{percent2(a.rate)} APY</Fact>);
  } else if (a.type === "cd") {
    if (a.rate) facts.push(<Fact key="y">{percent2(a.rate)} APY</Fact>);
    if (a.maturity_date) facts.push(<Fact key="m">Matures {dateLong(a.maturity_date)}</Fact>);
  }
  return {
    key: a.id,
    name: a.type === "ira" ? `${title(a.subtype || "")} IRA`.trim() : NAMES[a.type] ?? title(a.type),
    sub: a.subtype && a.type !== "ira" && a.type !== "credit_card" && a.subtype !== "estimated" ? title(a.subtype) : undefined,
    facts, amount: a.balance, payoff: a.type === "credit_card" || a.type === "auto_loan" || a.type === "personal_loan" || a.type === "mortgage", estimated: a.type === "home" || a.type === "vehicle",
  } as Row;
}

function build(profile: Profile): Group[] {
  const of = (...t: string[]) => profile.accounts.filter((a) => t.includes(a.type) && a.balance > 0);
  const mk = (types: string[], owed = false) => of(...types).map((a) => ({ ...row(a, profile), owed }));
  return [
    { title: "Cash", rows: mk(["checking", "savings", "cd"]) },
    { title: "Credit cards", rows: mk(["credit_card"], true) },
    { title: "Loans", rows: mk(["auto_loan", "personal_loan", "mortgage"], true) },
    { title: "Investments & retirement", rows: mk(["ira"]) },
    { title: "Property", rows: mk(["home", "vehicle"]) },
  ].filter((g) => g.rows.length);
}

export default function AccountGroups({ load }: { load: Load<Profile> }) {
  const groups = load.data ? build(load.data) : [];
  return (
    <Section id="accounts-h" title="Accounts">
      {load.error && <Unavailable>Your accounts are unavailable right now.</Unavailable>}
      {!load.data && !load.error && <div className="space-y-3">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-10 w-full" />)}</div>}
      <div className="fade-in space-y-8">
        {groups.map((g, gi) => (
          <div key={g.title}>
            <div className="flex items-baseline justify-between border-b border-line pb-1.5">
              <h3 className="text-[15px] font-medium">{g.title}</h3>
              <span className="num text-[15px] font-medium">{money0(g.rows.reduce((n, r) => n + r.amount, 0))}{g.rows[0].owed && <span className="ml-1 text-[13px] font-normal text-ink-2">owed</span>}</span>
            </div>
            <ul>
              {g.rows.map((r) => (
                <li key={r.key} className="row-hover grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 border-b border-line-soft px-1 py-3" style={{ animationDelay: `${gi * 40}ms` }}>
                  <div className="min-w-0">
                    <span className="text-[16px]">{r.name}</span>
                    {r.sub && <span className="ml-2 text-[14px] text-ink-2">{r.sub}</span>}
                    {r.estimated && <span className="ml-2 rounded-[6px] border border-line-strong px-1.5 py-px text-[12px] text-ink-2">Estimated value</span>}
                    {r.facts.length > 0 && <div className="mt-0.5 flex flex-wrap items-center gap-x-4 gap-y-0.5">{r.facts}</div>}
                    {r.payoff && <Link href="/simulate/payoff" className="mt-1 inline-block text-[14px] font-medium text-navy underline-offset-2 hover:underline">See payoff options →</Link>}
                  </div>
                  <div className="num text-right text-[16px]">{money2(r.amount)}{r.owed && <span className="block text-[13px] text-ink-2">owed</span>}</div>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Section>
  );
}
