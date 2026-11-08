"use client";
import { useEffect, useState } from "react";
import { getTransactions, type Profile, type TransactionPage } from "@/lib/api";
import { dateLong, money2 } from "@/lib/format";

export type TxFilters = { account: string; flow: string; category: string; q: string; page: number };
const PAGE_SIZE = 50;

const FLOWS: [string, string][] = [["", "All"], ["income", "Income"], ["expense", "Spending"], ["internal_transfer", "Transfers"], ["non_cash", "Interest & returns"]];
const ACCOUNT_NAMES: Record<string, string> = {
  checking: "Checking", savings: "Savings", cd: "CD", ira: "IRA", credit_card: "Credit card", auto_loan: "Auto loan", personal_loan: "Personal loan", mortgage: "Mortgage",
};
const label = (c: string) => c.replace(/_/g, " ");

function Chip({ on, children, onClick }: { on: boolean; children: React.ReactNode; onClick: () => void }) {
  return (
    <button onClick={onClick} aria-pressed={on}
      className={`press rounded-full border px-3 py-1 text-[13px] ${on ? "border-navy bg-navy text-white" : "border-line-strong text-ink-2 hover:border-ink-3"}`}>
      {children}
    </button>
  );
}

export default function Transactions({ profile, filters, onFilters }: { profile: Profile; filters: TxFilters; onFilters: (patch: Partial<TxFilters>) => void }) {
  const [data, setData] = useState<TransactionPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState(filters.q);
  const id = profile.id;

  useEffect(() => {
    let live = true;
    getTransactions(id, {
      account_id: filters.account || undefined, flow_class: filters.flow || undefined, category: filters.category || undefined,
      q: filters.q || undefined, page: filters.page, page_size: PAGE_SIZE,
    }).then((d) => { if (live) { setData(d); setError(null); } }).catch((e) => live && setError(String(e)));
    return () => { live = false; };
  }, [id, filters.account, filters.flow, filters.category, filters.q, filters.page]);

  useEffect(() => {
    if (search === filters.q) return;
    const t = setTimeout(() => onFilters({ q: search, page: 1 }), 300);
    return () => clearTimeout(t);
  }, [search, filters.q, onFilters]);

  const accounts = profile.accounts.filter((a) => ACCOUNT_NAMES[a.type]);
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const from = data && data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const to = data ? Math.min(data.total, data.page * data.page_size) : 0;

  return (
    <section aria-labelledby="tx-title" className="rounded-[6px] border border-line bg-surface p-4 sm:p-6">
      <h2 id="tx-title" className="text-[20px] font-semibold">Transactions</h2>

      <div className="mt-4 space-y-3">
        <input
          type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search descriptions" aria-label="Search transactions"
          className="w-full rounded-[6px] border border-line-strong bg-bg px-3 py-2 text-[14px] outline-none focus:border-navy sm:max-w-[320px]"
        />
        <div className="flex flex-wrap gap-2" role="group" aria-label="Type">
          {FLOWS.map(([v, l]) => <Chip key={v} on={filters.flow === v} onClick={() => onFilters({ flow: v, page: 1 })}>{l}</Chip>)}
        </div>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Account">
          <Chip on={!filters.account} onClick={() => onFilters({ account: "", page: 1 })}>All accounts</Chip>
          {accounts.map((a) => <Chip key={a.id} on={filters.account === a.id} onClick={() => onFilters({ account: a.id, page: 1 })}>{ACCOUNT_NAMES[a.type]}</Chip>)}
        </div>
        {filters.category && (
          <div className="flex items-center gap-2 text-[13px] text-ink-2">
            Category <Chip on onClick={() => onFilters({ category: "", page: 1 })}>{label(filters.category)} ✕</Chip>
          </div>
        )}
      </div>

      {error && <p className="mt-4 text-[14px] text-[#b4423a]">Could not load transactions: {error}</p>}

      <div className={`mt-4 transition-opacity duration-150 ${data ? "" : "opacity-50"}`} style={{ transitionTimingFunction: "var(--ease)" }}>
        <table className="stack w-full border-collapse text-left text-[14px]">
          <thead>
            <tr className="label !text-[12px]">
              <th className="border-b border-line-strong py-2 pr-4 font-normal">Date</th>
              <th className="border-b border-line-strong py-2 pr-4 font-normal">Description</th>
              <th className="border-b border-line-strong py-2 pr-4 font-normal">Category</th>
              <th className="border-b border-line-strong py-2 text-right font-normal">Amount</th>
            </tr>
          </thead>
          <tbody>
            {data?.items.map((t) => {
              const inflow = t.flow_class === "income";
              const signed = t.flow_class === "income" ? t.amount : t.flow_class === "expense" ? -t.amount : t.balance_change;
              return (
                <tr key={t.transaction_id} className="row-hover">
                  <td className="mono whitespace-nowrap border-b border-line py-2.5 pr-4 text-[13px] text-ink-2">{dateLong(t.posted_date)}</td>
                  <td className="border-b border-line py-2.5 pr-4">
                    <div className="text-ink">{t.description}</div>
                    <div className="text-[12px] text-ink-3">{ACCOUNT_NAMES[t.account_type] ?? t.account_type}</div>
                  </td>
                  <td className="border-b border-line py-2.5 pr-4">
                    <button onClick={() => onFilters({ category: t.category, page: 1 })} className="press rounded-[6px] text-[13px] capitalize text-ink-2 underline decoration-line-strong underline-offset-2">
                      {label(t.category)}
                    </button>
                  </td>
                  <td className={`amt mono border-b border-line py-2.5 text-right ${inflow ? "text-good" : signed > 0 && t.flow_class !== "expense" ? "text-ink-2" : "text-ink"}`}>
                    {signed > 0 ? "+" : signed < 0 ? "−" : ""}{money2(Math.abs(signed))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {data && data.items.length === 0 && <p className="py-8 text-center text-[14px] text-ink-3">No transactions match these filters.</p>}
      </div>

      <div className="mt-4 flex items-center justify-between text-[13px] text-ink-2">
        <span className="mono">{data ? `${from}–${to} of ${data.total.toLocaleString("en-US")}` : "Loading…"}</span>
        <div className="flex items-center gap-2">
          <button disabled={filters.page <= 1} onClick={() => onFilters({ page: filters.page - 1 })} className="press rounded-[6px] border border-line-strong px-3 py-1 disabled:opacity-40">Previous</button>
          <span className="mono">{filters.page} / {pages}</span>
          <button disabled={filters.page >= pages} onClick={() => onFilters({ page: filters.page + 1 })} className="press rounded-[6px] border border-line-strong px-3 py-1 disabled:opacity-40">Next</button>
        </div>
      </div>
    </section>
  );
}
