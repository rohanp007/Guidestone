"use client";
import { dateLong } from "@/lib/format";

const NAV: { label: string; ready: boolean }[] = [
  { label: "Accounts", ready: true }, { label: "Simulate", ready: false }, { label: "Plan", ready: false },
];

export default function Header({ name, asOf, onSignOut }: { name: string; asOf: string; onSignOut: () => void }) {
  return (
    <header className="border-b border-line-strong">
      <div className="mx-auto flex max-w-[1120px] flex-wrap items-center justify-between gap-x-6 gap-y-3 px-4 py-4 sm:px-6">
        <div className="text-[18px] font-semibold tracking-tight text-navy">Guidestone</div>
        <nav aria-label="Sections" className="order-3 flex w-full gap-6 text-[15px] sm:order-none sm:w-auto">
          {NAV.map((n) => n.ready
            ? <a key={n.label} href="/accounts" aria-current="page" className="border-b-2 border-navy pb-0.5 font-medium text-navy">{n.label}</a>
            : <span key={n.label} aria-disabled="true" className="pb-0.5 text-ink-2">{n.label} <span className="rounded-[6px] border border-line-strong px-1.5 py-px text-[11px] uppercase tracking-wide">soon</span></span>)}
        </nav>
        <div className="flex items-center gap-4 text-[15px]">
          <span className="text-right leading-tight"><span className="block font-medium">{name}</span>{asOf && <span className="block text-[13px] text-ink-2">As of {dateLong(asOf)}</span>}</span>
          <button type="button" onClick={onSignOut} className="press rounded-[6px] border border-line-strong bg-surface px-3 py-1.5 font-medium hover:border-navy">Sign out</button>
        </div>
      </div>
    </header>
  );
}
