"use client";
import Link from "next/link";
import { dateLong } from "@/lib/format";

export type NavId = "accounts" | "simulate";
const NAV: { id: NavId | "plan"; label: string; href?: string }[] = [
  { id: "accounts", label: "Accounts", href: "/accounts" }, { id: "simulate", label: "Simulate", href: "/simulate" }, { id: "plan", label: "Plan" },
];

export default function Header({ name, asOf, onSignOut, active = "accounts" }: { name: string; asOf: string; onSignOut: () => void; active?: NavId }) {
  return (
    <header className="border-b border-line-strong">
      <div className="mx-auto flex max-w-[1120px] flex-wrap items-center justify-between gap-x-6 gap-y-3 px-4 py-4 sm:px-6">
        <div className="text-[18px] font-semibold tracking-tight text-navy">Guidestone</div>
        <nav aria-label="Sections" className="order-3 flex w-full gap-6 text-[15px] sm:order-none sm:w-auto">
          {NAV.map((n) => n.href
            ? <Link key={n.label} href={n.href} aria-current={n.id === active ? "page" : undefined} className={`pb-0.5 ${n.id === active ? "border-b-2 border-navy font-medium text-navy" : "text-ink-2 hover:text-navy"}`}>{n.label}</Link>
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
