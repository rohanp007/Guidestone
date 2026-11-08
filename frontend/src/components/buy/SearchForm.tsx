"use client";
import { useId, useState } from "react";
import { PRIORITIES, PRIORITY_LABEL, SEGMENT_LABEL, type BuyParams, type BuyView, type Priority, type Segment } from "@/lib/buy";
import { money0 } from "@/lib/format";

const SEGMENTS: Segment[] = ["compact_suv", "midsize_sedan"];

export default function SearchForm({ onSubmit, busy, query }: { onSubmit: (p: BuyParams) => void; busy: boolean; query: BuyView["query"] | null }) {
  const budgetId = useId();
  const [segment, setSegment] = useState<Segment>("compact_suv");
  const [budget, setBudget] = useState("");
  const [priorities, setPriorities] = useState<Priority[]>([]);
  const toggle = (p: Priority) => setPriorities((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  const n = Number(budget.replace(/[^0-9.]/g, ""));
  const submit = (e: React.FormEvent) => { e.preventDefault(); onSubmit({ segment, budget: budget.trim() && Number.isFinite(n) ? n : null, priorities }); };
  const searched = query && [SEGMENT_LABEL[query.segment], query.budgetBand !== null ? `budget up to ${money0(query.budgetBand)}` : "no budget", query.priorities.length ? `priorities: ${query.priorities.map((p) => PRIORITY_LABEL[p]).join(", ")}` : "no priorities"].join(" · ");

  return (
    <form onSubmit={submit} aria-label="Find a car" className="space-y-6">
      <fieldset>
        <legend className="text-[15px] font-medium">Type of car</legend>
        <div className="mt-2 flex flex-wrap gap-2">
          {SEGMENTS.map((s) => (
            <label key={s} className="press cursor-pointer rounded-[6px] border border-line-strong bg-surface px-3 py-2 text-[15px] has-[:checked]:border-navy has-[:checked]:bg-navy-tint has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-orange">
              <input type="radio" name="segment" value={s} checked={segment === s} onChange={() => setSegment(s)} className="sr-only" />{SEGMENT_LABEL[s]}
            </label>
          ))}
        </div>
      </fieldset>
      <div>
        <label htmlFor={budgetId} className="text-[15px] font-medium">Max budget</label>
        <div className="relative mt-2 w-44">
          <span aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-2">$</span>
          <input id={budgetId} inputMode="numeric" value={budget} placeholder="35000" onChange={(e) => setBudget(e.target.value)}
            className="num w-full rounded-[6px] border border-line-strong bg-surface py-2 pl-6 pr-3 text-right text-[16px] outline-none focus-visible:border-navy" />
        </div>
      </div>
      <fieldset>
        <legend className="text-[15px] font-medium">What matters most <span className="font-normal text-ink-2">(optional)</span></legend>
        <div className="mt-2 flex flex-wrap gap-2">
          {PRIORITIES.map((p) => (
            <button key={p} type="button" aria-pressed={priorities.includes(p)} onClick={() => toggle(p)}
              className={`press rounded-full border px-3 py-1.5 text-[14px] ${priorities.includes(p) ? "border-navy bg-navy text-white" : "border-line-strong bg-surface text-ink-2"}`}>{PRIORITY_LABEL[p]}</button>
          ))}
        </div>
      </fieldset>
      <div className="flex flex-wrap items-center gap-4">
        <button type="submit" disabled={busy} className="press rounded-[6px] bg-orange px-5 py-2.5 text-[16px] font-semibold text-navy disabled:opacity-60">Find options</button>
        <span className="text-[14px] text-ink-2">Financing is a 60-month loan.</span>
      </div>
      <div className="rounded-[6px] border border-line bg-surface px-4 py-3 text-[14px] text-ink-2">
        <p className="label !text-[11px]">What we searched for</p>
        <p className="mt-1 text-ink">{searched ?? "Your segment, a budget band and your priorities, and nothing else."}</p>
        <p className="mt-1">Your name, balances and income are never sent to the research step.</p>
      </div>
    </form>
  );
}
