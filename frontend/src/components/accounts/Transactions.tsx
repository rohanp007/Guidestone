"use client";
import { useId, useMemo, useState } from "react";
import type { Transaction } from "@/lib/api";
import { dateShort, money2 } from "@/lib/format";
import { categoryName } from "./Spending";
import { Section, Skeleton, Unavailable, type Load } from "./ui";

const PAGE = 25;
const LIABILITY = new Set(["credit_card", "auto_loan", "personal_loan", "mortgage"]);
const ACCOUNT: Record<string, string> = { checking: "Checking", savings: "Savings", cd: "CD", ira: "IRA", credit_card: "Credit card", auto_loan: "Auto loan", personal_loan: "Personal loan", mortgage: "Mortgage" };

/** Money in is positive, money out negative, whatever the account type (on a card, a charge raises the balance but is money out). */
const signed = (t: Transaction) => (LIABILITY.has(t.account_type) ? -t.balance_change : t.balance_change);

export default function Transactions({ load }: { load: Load<Transaction[]> }) {
  const catId = useId(), qId = useId();
  const [category, setCategory] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const all = load.data;
  const categories = useMemo(() => [...new Set((all ?? []).map((t) => t.category))].sort(), [all]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (all ?? []).filter((t) => (!category || t.category === category) && (!needle || t.description.toLowerCase().includes(needle)));
  }, [all, category, q]);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  const cur = Math.min(page, pages);
  const shown = rows.slice((cur - 1) * PAGE, cur * PAGE);
  const field = "mt-1 w-full rounded-[6px] border border-line-strong bg-surface px-3 py-2 text-[15px] outline-none focus-visible:border-navy";

  return (
    <Section id="tx-h" title="Recent transactions" aside="Last 60 days">
      {load.error && <Unavailable>Transactions are unavailable right now.</Unavailable>}
      {!all && !load.error && <div className="space-y-2">{[0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-9 w-full" />)}</div>}
      {all && (
        <div className="fade-in">
          <div className="grid gap-4 sm:grid-cols-[1fr_16rem]">
            <div><label htmlFor={qId} className="label">Search</label>
              <input id={qId} type="search" value={q} placeholder="Description" onChange={(e) => { setQ(e.target.value); setPage(1); }} className={field} /></div>
            <div><label htmlFor={catId} className="label">Category</label>
              <select id={catId} value={category} onChange={(e) => { setCategory(e.target.value); setPage(1); }} className={field}>
                <option value="">All categories</option>
                {categories.map((c) => <option key={c} value={c}>{categoryName(c)}</option>)}
              </select></div>
          </div>
          <table className="stack mt-4 w-full text-left text-[15px]">
            <caption className="sr-only">Transactions from the last 60 days, newest first</caption>
            <thead><tr className="label border-b border-line-strong">
              <th scope="col" className="py-2 pr-4 font-normal">Date</th><th scope="col" className="pr-4 font-normal">Description</th>
              <th scope="col" className="pr-4 font-normal">Category</th><th scope="col" className="pr-4 font-normal">Account</th><th scope="col" className="text-right font-normal">Amount</th>
            </tr></thead>
            <tbody>
              {shown.map((t) => (
                <tr key={t.transaction_id} className="row-hover border-b border-line-soft">
                  <td className="num whitespace-nowrap py-2.5 pr-4 text-ink-2">{dateShort(t.posted_date)}</td>
                  <td className="pr-4">{t.description}</td>
                  <td className="pr-4 text-ink-2">{categoryName(t.category)}</td>
                  <td className="pr-4 text-ink-2">{ACCOUNT[t.account_type] ?? t.account_type}</td>
                  <td className="amt num text-right">{signed(t) > 0 ? "+" : signed(t) < 0 ? "−" : ""}{money2(Math.abs(signed(t)))}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="mt-4 text-[15px] text-ink-2">No transactions match.</p>}
          <div className="mt-4 flex items-center justify-between text-[14px] text-ink-2">
            <span className="num" aria-live="polite">{rows.length === 0 ? "0" : `${(cur - 1) * PAGE + 1}–${Math.min(cur * PAGE, rows.length)}`} of {rows.length}</span>
            <div className="flex items-center gap-2">
              <button type="button" disabled={cur <= 1} onClick={() => setPage(cur - 1)} className="press rounded-[6px] border border-line-strong bg-surface px-3 py-1.5 text-ink disabled:opacity-40">Previous</button>
              <span className="num">Page {cur} of {pages}</span>
              <button type="button" disabled={cur >= pages} onClick={() => setPage(cur + 1)} className="press rounded-[6px] border border-line-strong bg-surface px-3 py-1.5 text-ink disabled:opacity-40">Next</button>
            </div>
          </div>
        </div>
      )}
    </Section>
  );
}
